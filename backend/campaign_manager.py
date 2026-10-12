"""Campaign Manager - Enhanced campaign management with sequences and wizard."""

import threading
from datetime import datetime, timezone

from backend.app.campaign_status import (
    campaign_is_running,
    fail_campaign,
    get_campaign_status_snapshot,
    start_campaign_tracking,
)
from backend.app.crud import (
    count_due_followups,
    count_pending_leads,
    count_ready_outreach,
    get_followup_settings,
    leads_exist,
)
from backend.app.database import get_connection, row_to_dict
from backend.campaign_runner import run_campaign
from backend.config import get_missing_smtp_settings

CAMPAIGN_TABLE = "app_campaign"
SEQUENCE_TABLE = "app_email_sequence"
SEQUENCE_STEP_TABLE = "app_sequence_step"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_campaign_tables() -> None:
    with get_connection() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {CAMPAIGN_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                workspace_id INTEGER NULL,
                created_by INTEGER NULL,
                audience_filter TEXT NOT NULL DEFAULT '{{}}',
                sequence_id INTEGER NULL,
                scheduled_at DATETIME NULL,
                started_at DATETIME NULL,
                completed_at DATETIME NULL,
                total_leads INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                open_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {SEQUENCE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                workspace_id INTEGER NULL,
                created_by INTEGER NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {SEQUENCE_STEP_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                step_type VARCHAR(20) NOT NULL DEFAULT 'email',
                delay_days INTEGER NOT NULL DEFAULT 0,
                delay_hours INTEGER NOT NULL DEFAULT 0,
                subject_template TEXT NOT NULL DEFAULT '',
                body_template TEXT NOT NULL DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '{{}}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sequence_id) REFERENCES {SEQUENCE_TABLE}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{CAMPAIGN_TABLE}_status ON {CAMPAIGN_TABLE}(status)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{SEQUENCE_TABLE}_workspace ON {SEQUENCE_TABLE}(workspace_id)")


def list_campaigns(workspace_id: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if workspace_id:
            rows = conn.execute(
                f"SELECT * FROM {CAMPAIGN_TABLE} WHERE workspace_id = ? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {CAMPAIGN_TABLE} ORDER BY created_at DESC"
            ).fetchall()
    return [row_to_dict(r) for r in rows]


def create_campaign(data: dict) -> dict:
    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO {CAMPAIGN_TABLE} (name, status, workspace_id, created_by, audience_filter, sequence_id, scheduled_at)
            VALUES (?, 'draft', ?, ?, ?, ?, ?)
            """,
            (
                data.get("name", "Untitled Campaign"),
                data.get("workspace_id"),
                data.get("created_by"),
                data.get("audience_filter", "{}"),
                data.get("sequence_id"),
                data.get("scheduled_at"),
            ),
        )
        row_id = cursor.lastrowid
        row = conn.execute(f"SELECT * FROM {CAMPAIGN_TABLE} WHERE id = ?", (row_id,)).fetchone()
    return row_to_dict(row) or {}


def get_campaign(campaign_id: int, workspace_id: int | None = None) -> dict | None:
    with get_connection() as conn:
        if workspace_id is None:
            row = conn.execute(
                f"SELECT * FROM {CAMPAIGN_TABLE} WHERE id = ?", (campaign_id,)
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM {CAMPAIGN_TABLE} WHERE id = ? AND workspace_id = ?",
                (campaign_id, workspace_id),
            ).fetchone()
    return row_to_dict(row) if row else None


def update_campaign_status(
    campaign_id: int,
    status: str,
    *,
    total_leads: int | None = None,
    sent_count: int | None = None,
    failed_count: int | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE {CAMPAIGN_TABLE}
            SET status = ?,
                total_leads = COALESCE(?, total_leads),
                sent_count = COALESCE(?, sent_count),
                failed_count = COALESCE(?, failed_count),
                started_at = CASE WHEN ? = 'running' THEN ? ELSE started_at END,
                completed_at = CASE WHEN ? IN ('completed', 'failed') THEN ? ELSE completed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                total_leads,
                sent_count,
                failed_count,
                status,
                _utcnow_iso(),
                status,
                _utcnow_iso(),
                _utcnow_iso(),
                campaign_id,
            ),
        )


def start_campaign_with_validation() -> dict:
    if not leads_exist():
        return {"error": "No leads found. Upload leads before starting a campaign.", "status": 400}

    if campaign_is_running():
        return {
            "error": "Campaign is already running.",
            "status": 409,
            "campaign": get_campaign_status_snapshot(),
        }

    followup_settings = get_followup_settings()
    pending_leads = count_pending_leads()
    due_followups = count_due_followups(followup_settings) if followup_settings.get("enabled") else 0
    ready_outreach = count_ready_outreach(followup_settings)

    if ready_outreach == 0:
        return {
            "error": "No pending leads or due follow-ups.",
            "status": 400,
        }

    missing_settings = get_missing_smtp_settings()
    if missing_settings:
        return {
            "error": f"Missing SMTP configuration: {', '.join(missing_settings)}",
            "status": 400,
        }

    started, campaign = start_campaign_tracking(total=ready_outreach)
    if not started:
        return {"error": "Campaign is already running.", "status": 409, "campaign": campaign}

    def task():
        try:
            run_campaign(use_csv_fallback=False, only_unsent=True)
        except Exception as exc:
            fail_campaign(str(exc))

    threading.Thread(target=task, daemon=True).start()

    return {
        "status": "campaign started",
        "campaign": campaign,
        "queue": {
            "pending_leads": pending_leads,
            "due_followups": due_followups,
            "total": ready_outreach,
        },
    }


def list_sequences(workspace_id: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if workspace_id:
            rows = conn.execute(
                f"SELECT * FROM {SEQUENCE_TABLE} WHERE workspace_id = ? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {SEQUENCE_TABLE} ORDER BY created_at DESC"
            ).fetchall()
    return [row_to_dict(r) for r in rows]


def create_sequence(data: dict) -> dict:
    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO {SEQUENCE_TABLE} (name, description, workspace_id, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (
                data.get("name", "Untitled Sequence"),
                data.get("description", ""),
                data.get("workspace_id"),
                data.get("created_by"),
            ),
        )
        seq_id = cursor.lastrowid
        row = conn.execute(f"SELECT * FROM {SEQUENCE_TABLE} WHERE id = ?", (seq_id,)).fetchone()
    return row_to_dict(row) or {}


def get_sequence_steps(sequence_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM {SEQUENCE_STEP_TABLE} WHERE sequence_id = ? ORDER BY step_number",
            (sequence_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def add_sequence_step(sequence_id: int, data: dict) -> dict:
    with get_connection() as conn:
        existing = conn.execute(
            f"SELECT MAX(step_number) AS max_step FROM {SEQUENCE_STEP_TABLE} WHERE sequence_id = ?",
            (sequence_id,),
        ).fetchone()
        next_step = int(existing["max_step"] or 0) + 1 if existing else 1

        cursor = conn.execute(
            f"""
            INSERT INTO {SEQUENCE_STEP_TABLE}
            (sequence_id, step_number, step_type, delay_days, delay_hours, subject_template, body_template, conditions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence_id,
                next_step,
                data.get("step_type", "email"),
                data.get("delay_days", 0),
                data.get("delay_hours", 0),
                data.get("subject_template", ""),
                data.get("body_template", ""),
                data.get("conditions", "{}"),
            ),
        )
        row = conn.execute(
            f"SELECT * FROM {SEQUENCE_STEP_TABLE} WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return row_to_dict(row) or {}
