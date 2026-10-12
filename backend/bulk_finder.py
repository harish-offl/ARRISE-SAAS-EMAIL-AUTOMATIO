"""Bulk Email Finder with queue system, pause/resume, and retry support."""

import threading
import time
from datetime import datetime, timezone
from enum import Enum

from backend.app.database import get_connection, row_to_dict
from backend.email_finder import find_emails, verify_email

BULK_JOB_TABLE = "app_bulk_email_job"
BULK_RESULT_TABLE = "app_bulk_email_result"

_bulk_jobs: dict[int, dict] = {}
_bulk_lock = threading.Lock()


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_bulk_tables() -> None:
    with get_connection() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {BULK_JOB_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL DEFAULT 'Bulk Email Find',
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                total_items INTEGER NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                found_items INTEGER NOT NULL DEFAULT 0,
                failed_items INTEGER NOT NULL DEFAULT 0,
                duplicate_items INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_by INTEGER NULL,
                started_at DATETIME NULL,
                completed_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {BULK_RESULT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                prospect_name VARCHAR(200) NOT NULL DEFAULT '',
                company VARCHAR(200) NOT NULL DEFAULT '',
                domain VARCHAR(200) NOT NULL DEFAULT '',
                emails_found TEXT NOT NULL DEFAULT '[]',
                mx_valid INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES {BULK_JOB_TABLE}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{BULK_JOB_TABLE}_status ON {BULK_JOB_TABLE}(status)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{BULK_RESULT_TABLE}_job ON {BULK_RESULT_TABLE}(job_id)")


def create_bulk_job(name: str, prospects: list[dict], created_by: int | None = None) -> dict:
    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO {BULK_JOB_TABLE} (name, status, total_items, created_by)
            VALUES (?, 'pending', ?, ?)
            """,
            (name, len(prospects), created_by),
        )
        job_id = cursor.lastrowid

        for prospect in prospects:
            conn.execute(
                f"""
                INSERT INTO {BULK_RESULT_TABLE} (job_id, prospect_name, company, domain, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    job_id,
                    prospect.get("name", ""),
                    prospect.get("company", ""),
                    prospect.get("domain", ""),
                ),
            )

    job = get_bulk_job(job_id)
    with _bulk_lock:
        _bulk_jobs[job_id] = {
            "status": "pending",
            "thread": None,
            "cancelled": False,
            "paused": False,
        }

    return job


def get_bulk_job(job_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(f"SELECT * FROM {BULK_JOB_TABLE} WHERE id = ?", (job_id,)).fetchone()
    return row_to_dict(row) if row else None


def get_bulk_results(job_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM {BULK_RESULT_TABLE} WHERE job_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def list_bulk_jobs(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM {BULK_JOB_TABLE} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def _process_bulk_job(job_id: int) -> None:
    with get_connection() as conn:
        prospects = conn.execute(
            f"SELECT * FROM {BULK_RESULT_TABLE} WHERE job_id = ? AND status = 'pending' ORDER BY id",
            (job_id,),
        ).fetchall()

    found_count = 0
    failed_count = 0
    processed = 0

    for prospect in prospects:
        with _bulk_lock:
            job_state = _bulk_jobs.get(job_id, {})
            if job_state.get("cancelled"):
                _update_job_status(job_id, "cancelled")
                return
            while job_state.get("paused"):
                time.sleep(0.5)
                if job_state.get("cancelled"):
                    _update_job_status(job_id, "cancelled")
                    return

        try:
            name = prospect["prospect_name"]
            domain = prospect["domain"]
            company = prospect["company"]

            finder_request = type("Request", (), {"name": name, "company": company, "domain": domain, "job_title": ""})()
            result = find_emails(finder_request)

            emails_json = str([r["email"] for r in result.get("results", [])])
            mx_valid = 1 if result.get("mx_check", {}).get("valid") else 0
            status = "found" if result.get("results") else "not_found"

            with get_connection() as conn:
                conn.execute(
                    f"""
                    UPDATE {BULK_RESULT_TABLE}
                    SET emails_found = ?, mx_valid = ?, status = ?
                    WHERE id = ?
                    """,
                    (emails_json, mx_valid, status, prospect["id"]),
                )

            if result.get("results"):
                found_count += 1
            else:
                failed_count += 1

        except Exception as e:
            with get_connection() as conn:
                conn.execute(
                    f"UPDATE {BULK_RESULT_TABLE} SET status = 'error', error = ? WHERE id = ?",
                    (str(e)[:500], prospect["id"]),
                )
            failed_count += 1

        processed += 1

        with get_connection() as conn:
            conn.execute(
                f"""
                UPDATE {BULK_JOB_TABLE}
                SET processed_items = ?, found_items = ?, failed_items = ?, updated_at = ?
                WHERE id = ?
                """,
                (processed, found_count, failed_count, _utcnow_iso(), job_id),
            )

        time.sleep(0.1)

    _update_job_status(job_id, "completed")


def _update_job_status(job_id: int, status: str) -> None:
    with get_connection() as conn:
        updates = {"status": status, "updated_at": _utcnow_iso()}
        if status in ("completed", "failed", "cancelled"):
            updates["completed_at"] = _utcnow_iso()
        if status == "running":
            updates["started_at"] = _utcnow_iso()

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE {BULK_JOB_TABLE} SET {set_clause} WHERE id = ?",
            (*updates.values(), job_id),
        )


def start_bulk_job(job_id: int) -> bool:
    with _bulk_lock:
        if job_id in _bulk_jobs and _bulk_jobs[job_id].get("status") == "running":
            return False

    _update_job_status(job_id, "running")

    with _bulk_lock:
        _bulk_jobs[job_id] = {"status": "running", "cancelled": False, "paused": False}

    thread = threading.Thread(target=_process_bulk_job, args=(job_id,), daemon=True)
    thread.start()

    with _bulk_lock:
        _bulk_jobs[job_id]["thread"] = thread

    return True


def pause_bulk_job(job_id: int) -> bool:
    with _bulk_lock:
        if job_id in _bulk_jobs:
            _bulk_jobs[job_id]["paused"] = True
            _bulk_jobs[job_id]["status"] = "paused"
    _update_job_status(job_id, "paused")
    return True


def resume_bulk_job(job_id: int) -> bool:
    with _bulk_lock:
        if job_id in _bulk_jobs:
            _bulk_jobs[job_id]["paused"] = False
            _bulk_jobs[job_id]["status"] = "running"
    _update_job_status(job_id, "running")

    thread = threading.Thread(target=_process_bulk_job, args=(job_id,), daemon=True)
    thread.start()

    with _bulk_lock:
        _bulk_jobs[job_id]["thread"] = thread

    return True


def cancel_bulk_job(job_id: int) -> bool:
    with _bulk_lock:
        if job_id in _bulk_jobs:
            _bulk_jobs[job_id]["cancelled"] = True
    _update_job_status(job_id, "cancelled")
    return True


def get_bulk_job_progress(job_id: int) -> dict:
    job = get_bulk_job(job_id)
    if not job:
        return {"error": "Job not found"}

    progress = 0
    if job["total_items"] > 0:
        progress = round((job["processed_items"] / job["total_items"]) * 100, 1)

    estimated_time = None
    if job["status"] == "running" and job["processed_items"] > 0 and job["started_at"]:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds()
        rate = job["processed_items"] / elapsed
        remaining = job["total_items"] - job["processed_items"]
        estimated_time = round(remaining / rate, 0) if rate > 0 else None

    return {
        **job,
        "progress_percent": progress,
        "estimated_time_seconds": estimated_time,
    }
