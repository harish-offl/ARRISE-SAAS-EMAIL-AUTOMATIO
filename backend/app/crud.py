import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from backend.config import get_sender_identity_defaults
from backend.app.database import (
    DEFAULT_SENDER_PROFILE,
    DEFAULT_FOLLOWUP_SETTINGS,
    EMAIL_EVENT_TABLE,
    EMAIL_OPEN_TABLE,
    FOLLOWUP_SETTINGS_TABLE,
    SENDER_PROFILE_TABLE,
    TABLE_NAME,
    get_connection,
    initialize_database,
    row_to_dict,
)


LEAD_FIELDS = ("name", "email", "niche", "industry", "phone", "company")
FOLLOWUP_STEP_FIELDS = (
    ("followup_1_delay_days", "followup_1_date", "followup_1"),
    ("followup_2_delay_days", "followup_2_date", "followup_2"),
    ("final_bump_delay_days", "final_bump_date", "final_bump"),
)
MAX_DELAY_DAYS = 90


initialize_database()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_date_value(value, field_name: str) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format.") from exc
    return parsed.isoformat()


def _normalize_thread_subject(subject: str) -> str:
    normalized = (subject or "").strip()
    while normalized.lower().startswith("re:"):
        normalized = normalized[3:].strip()
    return normalized


def _body_preview(body: str, limit: int = 500) -> str:
    preview = " ".join(str(body or "").split())
    return preview[:limit]


def _lead_status_for_reply_type(reply_type: str) -> str:
    """Derive a consolidated lead status from a classified reply type.

    This is the single source of truth for how a reply maps to the lead's
    business status. Nothing else in the system should compute this separately.
    """
    normalized = (reply_type or "").strip().lower()
    if normalized in {"not_interested"}:
        return "not_interested"
    if normalized in {"unsubscribe", "opt_out"}:
        return "unsubscribed"
    if normalized in {"bounce"}:
        return "bounced"
    if normalized in {"out_of_office", "auto_reply"}:
        return "auto_replied"
    if normalized:
        return "replied"
    return "replied"


def _touch_label(touch_number: int) -> str:
    if touch_number <= 0:
        return "initial"
    if touch_number == 1:
        return "followup_1"
    if touch_number == 2:
        return "followup_2"
    return "final_bump"


def _lead_select_fields() -> str:
    return (
        "id, name, email, niche, industry, phone, company, sent_at, last_status, last_error, "
        "last_contacted_at, next_followup_at, followup_count, sequence_status, "
        "reply_received_at, opt_out_at, sequence_completed_at, thread_subject, last_message_id, "
        "first_open_at, last_open_at, open_count, reply_type, reply_summary, archived_at"
    )


def _read_followup_settings(connection) -> dict:
    row = connection.execute(
        f"""
        SELECT
            enabled,
            followup_1_delay_days,
            followup_2_delay_days,
            final_bump_delay_days,
            followup_1_date,
            followup_2_date,
            final_bump_date
        FROM {FOLLOWUP_SETTINGS_TABLE}
        WHERE id = 1
        """
    ).fetchone()

    settings = dict(DEFAULT_FOLLOWUP_SETTINGS)
    if row:
        settings.update(
            {
                "enabled": bool(row["enabled"]),
                "followup_1_delay_days": int(row["followup_1_delay_days"]),
                "followup_2_delay_days": int(row["followup_2_delay_days"]),
                "final_bump_delay_days": int(row["final_bump_delay_days"]),
                "followup_1_date": row["followup_1_date"] or None,
                "followup_2_date": row["followup_2_date"] or None,
                "final_bump_date": row["final_bump_date"] or None,
            }
        )
    return settings


def _coerce_day_delay(value, field_name: str) -> int:
    try:
        delay = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if delay < 0 or delay > MAX_DELAY_DAYS:
        raise ValueError(f"{field_name} must be between 0 and {MAX_DELAY_DAYS}.")
    return delay


def _normalize_followup_settings(payload: dict, base: dict) -> dict:
    settings = {
        "enabled": bool(payload.get("enabled", base["enabled"])),
        "followup_1_delay_days": _coerce_day_delay(
            payload.get("followup_1_delay_days", base["followup_1_delay_days"]),
            "followup_1_delay_days",
        ),
        "followup_2_delay_days": _coerce_day_delay(
            payload.get("followup_2_delay_days", base["followup_2_delay_days"]),
            "followup_2_delay_days",
        ),
        "final_bump_delay_days": _coerce_day_delay(
            payload.get("final_bump_delay_days", base["final_bump_delay_days"]),
            "final_bump_delay_days",
        ),
        "followup_1_date": _normalize_date_value(
            payload.get("followup_1_date", base.get("followup_1_date")),
            "followup_1_date",
        ),
        "followup_2_date": _normalize_date_value(
            payload.get("followup_2_date", base.get("followup_2_date")),
            "followup_2_date",
        ),
        "final_bump_date": _normalize_date_value(
            payload.get("final_bump_date", base.get("final_bump_date")),
            "final_bump_date",
        ),
    }

    fixed_dates = [
        settings["followup_1_date"],
        settings["followup_2_date"],
        settings["final_bump_date"],
    ]
    previous_date = None
    for fixed in fixed_dates:
        if not fixed:
            continue
        parsed = date.fromisoformat(fixed)
        if previous_date and parsed < previous_date:
            raise ValueError("Follow-up dates must be in chronological order.")
        previous_date = parsed

    steps = get_followup_steps(settings)
    if settings["enabled"] and not steps:
        raise ValueError("Enable at least one follow-up delay or set a follow-up date before turning follow-ups on.")
    return settings


def get_followup_steps(settings: dict | None = None) -> list[dict]:
    settings = dict(DEFAULT_FOLLOWUP_SETTINGS if settings is None else settings)

    steps = []
    encountered_disabled_step = False
    for index, (delay_field, date_field, touch_type) in enumerate(FOLLOWUP_STEP_FIELDS, start=1):
        delay_days = int(settings.get(delay_field, 0) or 0)
        fixed_date = _normalize_date_value(settings.get(date_field), date_field)
        enabled_step = delay_days > 0 or bool(fixed_date)
        if not enabled_step:
            encountered_disabled_step = True
            continue
        if encountered_disabled_step:
            raise ValueError("Follow-up steps must be enabled in order without gaps (by date or delay).")
        steps.append(
            {
                "number": index,
                "touch_type": touch_type,
                "delay_days": delay_days,
                "fixed_date": fixed_date,
            }
        )

    return steps


def _next_followup_at(reference_time: str, completed_followups: int, settings: dict) -> str | None:
    if not settings.get("enabled"):
        return None

    steps = get_followup_steps(settings)
    if completed_followups >= len(steps):
        return None

    step = steps[completed_followups]
    fixed_date = step.get("fixed_date")
    if fixed_date:
        fixed_date_obj = date.fromisoformat(fixed_date)
        fixed_datetime = datetime.combine(fixed_date_obj, time(hour=9, minute=0), tzinfo=timezone.utc)
        return fixed_datetime.isoformat()

    base_time = _parse_datetime(reference_time) or _now()
    delay_days = step["delay_days"]
    return (base_time + timedelta(days=delay_days)).isoformat()


def _get_followup_queue_summary(connection, settings: dict) -> dict:
    if not settings.get("enabled"):
        return {
            "due_count": 0,
            "scheduled_count": 0,
            "active_count": 0,
            "next_due_at": None,
        }

    now_iso = _now_iso()
    max_followups = len(get_followup_steps(settings))
    rows = connection.execute(
        f"""
        SELECT followup_count, next_followup_at
        FROM {TABLE_NAME}
        WHERE sent_at IS NOT NULL
          AND next_followup_at IS NOT NULL
          AND sequence_status = 'active'
          AND reply_received_at IS NULL
          AND opt_out_at IS NULL
        """
    ).fetchall()

    due_count = 0
    scheduled_count = 0
    next_due_at = None

    for row in rows:
        if int(row["followup_count"] or 0) >= max_followups:
            continue

        next_followup_at = row["next_followup_at"]
        if not next_followup_at:
            continue

        if next_followup_at <= now_iso:
            due_count += 1
        else:
            scheduled_count += 1
            if next_due_at is None or next_followup_at < next_due_at:
                next_due_at = next_followup_at

    return {
        "due_count": due_count,
        "scheduled_count": scheduled_count,
        "active_count": due_count + scheduled_count,
        "next_due_at": next_due_at,
    }


def get_followup_settings() -> dict:
    with get_connection() as connection:
        settings = _read_followup_settings(connection)
        return {**settings, **_get_followup_queue_summary(connection, settings)}


def update_followup_settings(payload: dict) -> dict:
    with get_connection() as connection:
        current = _read_followup_settings(connection)
        settings = _normalize_followup_settings(payload or {}, current)

        connection.execute(
            f"""
            UPDATE {FOLLOWUP_SETTINGS_TABLE}
            SET enabled = ?,
                followup_1_delay_days = ?,
                followup_2_delay_days = ?,
                final_bump_delay_days = ?,
                followup_1_date = ?,
                followup_2_date = ?,
                final_bump_date = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                int(settings["enabled"]),
                settings["followup_1_delay_days"],
                settings["followup_2_delay_days"],
                settings["final_bump_delay_days"],
                settings["followup_1_date"],
                settings["followup_2_date"],
                settings["final_bump_date"],
                _now_iso(),
            ),
        )

        if not settings["enabled"]:
            connection.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET next_followup_at = NULL,
                    sequence_status = CASE
                        WHEN sent_at IS NULL THEN 'pending'
                        WHEN reply_received_at IS NOT NULL OR opt_out_at IS NOT NULL THEN 'stopped'
                        ELSE 'completed'
                    END,
                    sequence_completed_at = CASE
                        WHEN sent_at IS NULL THEN sequence_completed_at
                        WHEN reply_received_at IS NOT NULL OR opt_out_at IS NOT NULL THEN sequence_completed_at
                        ELSE COALESCE(sequence_completed_at, last_contacted_at, sent_at)
                    END
                WHERE sequence_status = 'active'
                """
            )

        return {**settings, **_get_followup_queue_summary(connection, settings)}


def list_leads(*, include_archived: bool = False) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            {"WHERE archived_at IS NOT NULL" if include_archived else "WHERE archived_at IS NULL"}
            ORDER BY id
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_campaign_leads(*, only_unsent: bool = True) -> list[dict]:
    if only_unsent:
        return list_pending_initial_leads()
    return list_leads()


def list_pending_initial_leads() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE sent_at IS NULL
              AND reply_received_at IS NULL
              AND opt_out_at IS NULL
              AND archived_at IS NULL
              AND COALESCE(sequence_status, 'pending') != 'paused'
            ORDER BY id
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_due_followup_leads(settings: dict | None = None) -> list[dict]:
    resolved_settings = get_followup_settings() if settings is None else settings
    if not resolved_settings.get("enabled"):
        return []

    max_followups = len(get_followup_steps(resolved_settings))
    if max_followups == 0:
        return []

    now_iso = _now_iso()
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE sent_at IS NOT NULL
              AND next_followup_at IS NOT NULL
              AND next_followup_at <= ?
              AND sequence_status = 'active'
              AND reply_received_at IS NULL
              AND opt_out_at IS NULL
              AND archived_at IS NULL
            ORDER BY next_followup_at, id
            """,
            (now_iso,),
        ).fetchall()

    leads = []
    for row in rows:
        lead = row_to_dict(row) or {}
        if int(lead.get("followup_count") or 0) >= max_followups:
            continue
        leads.append(lead)
    return leads


def list_outreach_queue(settings: dict | None = None) -> list[dict]:
    resolved_settings = get_followup_settings() if settings is None else settings

    queue = []
    for lead in list_due_followup_leads(resolved_settings):
        touch_number = int(lead.get("followup_count") or 0) + 1
        lead["touch_number"] = touch_number
        lead["touch_type"] = _touch_label(touch_number)
        lead["scheduled_for"] = lead.get("next_followup_at")
        queue.append(lead)

    for lead in list_pending_initial_leads():
        lead["touch_number"] = 0
        lead["touch_type"] = "initial"
        lead["scheduled_for"] = None
        queue.append(lead)

    return queue


def get_lead_by_email(email: str) -> dict | None:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return None

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE lower(email) = lower(?)
            """,
            (normalized_email,),
        ).fetchone()
    return row_to_dict(row)


def get_lead_by_message_ids(message_ids: list[str] | None) -> dict | None:
    references = [str(ref or "").strip() for ref in (message_ids or []) if str(ref or "").strip()]
    if not references:
        return None

    placeholders = ",".join("?" for _ in references)
    with get_connection() as connection:
        lead_rows = connection.execute(
            f"""
            SELECT DISTINCT lead_id
            FROM {EMAIL_EVENT_TABLE}
            WHERE message_id IN ({placeholders})
              AND status = 'sent'
              AND lead_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 2
            """,
            references,
        ).fetchall()
        if len(lead_rows) != 1:
            return None
        row = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE id = ?
            """,
            (int(lead_rows[0]["lead_id"]),),
        ).fetchone()
    return row_to_dict(row)

def find_unique_sent_lead_by_subject(subject: str) -> dict | None:
    normalized_subject = _normalize_thread_subject(subject)
    if not normalized_subject:
        return None

    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE sent_at IS NOT NULL
              AND reply_received_at IS NULL
              AND lower(thread_subject) = lower(?)
            ORDER BY id DESC
            LIMIT 2
            """,
            (normalized_subject,),
        ).fetchall()

    if len(rows) != 1:
        return None
    return row_to_dict(rows[0])

def reply_event_already_processed(*, provider: str = "imap", provider_message_id: str = "", message_id: str = "") -> bool:
    provider_value = _clean_text(provider or "imap", "imap", 32).lower()
    provider_message_id = _clean_text(provider_message_id or "", "", 254)
    message_id = _clean_text(message_id or "", "", 254)
    if not provider_message_id and not message_id:
        return False

    clauses = []
    params: list[str] = []
    if provider_message_id:
        clauses.append("(provider = ? AND provider_message_id = ?)")
        params.extend([provider_value, provider_message_id])
    if message_id:
        clauses.append("message_id = ?")
        params.append(message_id)

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT id
            FROM {EMAIL_EVENT_TABLE}
            WHERE status = 'reply_received'
              AND ({' OR '.join(clauses)})
            LIMIT 1
            """,
            params,
        ).fetchone()
    return row is not None


def create_lead(lead_data: dict) -> dict:
    normalized = _normalize_lead_payload(lead_data)
    with get_connection() as connection:
        cursor = connection.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                name,
                email,
                niche,
                industry,
                phone,
                company,
                last_status,
                last_error,
                followup_count,
                sequence_status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', '', 0, 'pending')
            """,
            (
                normalized["name"],
                normalized["email"],
                normalized["niche"],
                normalized["industry"],
                normalized["phone"],
                normalized["company"],
            ),
        )
        row_id = cursor.lastrowid
        row = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
    return row_to_dict(row) or {}


def update_lead(lead_id: int, lead_data: dict) -> dict | None:
    normalized = _normalize_lead_payload(lead_data)
    with get_connection() as connection:
        duplicate = connection.execute(
            f"SELECT id FROM {TABLE_NAME} WHERE lower(email) = lower(?) AND id != ?",
            (normalized["email"], lead_id),
        ).fetchone()
        if duplicate:
            raise ValueError("Another lead already uses this email address.")
        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET name = ?, email = ?, niche = ?, industry = ?, phone = ?, company = ?
            WHERE id = ? AND archived_at IS NULL
            """,
            (
                normalized["name"], normalized["email"], normalized["niche"],
                normalized["industry"], normalized["phone"], normalized["company"], lead_id,
            ),
        )
        if not cursor.rowcount:
            return None
        row = connection.execute(
            f"SELECT {_lead_select_fields()} FROM {TABLE_NAME} WHERE id = ?",
            (lead_id,),
        ).fetchone()
    return row_to_dict(row)


def archive_lead(lead_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET archived_at = ?, sequence_status = 'paused', next_followup_at = NULL
            WHERE id = ? AND archived_at IS NULL
            """,
            (_now_iso(), lead_id),
        )
    return cursor.rowcount > 0


def bulk_store_leads(rows: list[dict], *, replace_existing: bool) -> tuple[int, int]:
    created = 0
    updated = 0

    with get_connection() as connection:
        if replace_existing:
            connection.execute(f"DELETE FROM {TABLE_NAME}")

        for raw_row in rows:
            row = _normalize_lead_payload(raw_row)
            existing = connection.execute(
                f"SELECT id, sent_at FROM {TABLE_NAME} WHERE lower(email) = lower(?)",
                (row["email"],),
            ).fetchone()

            if existing:
                if existing["sent_at"] is None:
                    connection.execute(
                        f"""
                        UPDATE {TABLE_NAME}
                        SET name = ?,
                            niche = ?,
                            industry = ?,
                            phone = ?,
                            company = ?,
                            email = ?,
                            last_status = 'pending',
                            last_error = '',
                            last_contacted_at = NULL,
                            next_followup_at = NULL,
                            followup_count = 0,
                            sequence_status = 'pending',
                            reply_received_at = NULL,
                            opt_out_at = NULL,
                            sequence_completed_at = NULL,
                            thread_subject = '',
                            last_message_id = '',
                            first_open_at = NULL,
                            last_open_at = NULL,
                            open_count = 0,
                            reply_type = '',
                            reply_summary = '',
                            archived_at = NULL
                        WHERE id = ?
                        """,
                        (
                            row["name"],
                            row["niche"],
                            row["industry"],
                            row["phone"],
                            row["company"],
                            row["email"],
                            existing["id"],
                        ),
                    )
                else:
                    connection.execute(
                        f"""
                        UPDATE {TABLE_NAME}
                        SET name = ?,
                            niche = ?,
                            industry = ?,
                            phone = ?,
                            company = ?,
                            email = ?
                        WHERE id = ?
                        """,
                        (
                            row["name"],
                            row["niche"],
                            row["industry"],
                            row["phone"],
                            row["company"],
                            row["email"],
                            existing["id"],
                        ),
                    )
                updated += 1
            else:
                connection.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (
                        name,
                        email,
                        niche,
                        industry,
                        phone,
                        company,
                        last_status,
                        last_error,
                        followup_count,
                        sequence_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', '', 0, 'pending')
                    """,
                    (
                        row["name"],
                        row["email"],
                        row["niche"],
                        row["industry"],
                        row["phone"],
                        row["company"],
                    ),
                )
                created += 1

    return created, updated


def leads_exist() -> bool:
    with get_connection() as connection:
        row = connection.execute(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1").fetchone()
    return row is not None


def reset_failed_leads() -> int:
    """Reset all failed leads back to pending so they can be retried."""
    with get_connection() as connection:
        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = 'pending',
                last_error = '',
                sequence_status = 'pending',
                sent_at = NULL,
                last_contacted_at = NULL,
                next_followup_at = NULL,
                followup_count = 0
            WHERE last_status = 'failed'
              AND sent_at IS NULL
              AND archived_at IS NULL
            """
        )
        return cursor.rowcount


def count_pending_leads() -> int:
    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM {TABLE_NAME}
            WHERE sent_at IS NULL
              AND reply_received_at IS NULL
              AND opt_out_at IS NULL
              AND archived_at IS NULL
              AND COALESCE(sequence_status, 'pending') != 'paused'
            """
        ).fetchone()
    return int(row["count"] if row else 0)


def count_due_followups(settings: dict | None = None) -> int:
    return len(list_due_followup_leads(settings))


def count_ready_outreach(settings: dict | None = None) -> int:
    resolved_settings = get_followup_settings() if settings is None else settings
    due_count = count_due_followups(resolved_settings) if resolved_settings.get("enabled") else 0
    return count_pending_leads() + due_count


def get_thread_context(lead_id: int) -> dict:
    with get_connection() as connection:
        lead_row = connection.execute(
            f"""
            SELECT thread_subject, last_message_id
            FROM {TABLE_NAME}
            WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()
        reference_rows = connection.execute(
            f"""
            SELECT message_id
            FROM {EMAIL_EVENT_TABLE}
            WHERE lead_id = ?
              AND status = 'sent'
              AND message_id != ''
            ORDER BY id
            """,
            (lead_id,),
        ).fetchall()

    references = [row["message_id"] for row in reference_rows if row["message_id"]]
    return {
        "thread_subject": (lead_row["thread_subject"] if lead_row else "") or "",
        "last_message_id": (lead_row["last_message_id"] if lead_row else "") or "",
        "references": references,
    }


def record_outreach_success(
    lead_id: int,
    *,
    touch_type: str,
    touch_number: int,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str = "",
    tracking_token: str = "",
    copy_quality_score: int = 100,
    copy_quality_flags: str = "",
    scheduled_for: str | None = None,
    settings: dict | None = None,
) -> None:
    now_iso = _now_iso()
    resolved_settings = get_followup_settings() if settings is None else settings

    with get_connection() as connection:
        lead_row = connection.execute(
            f"""
            SELECT sent_at, thread_subject
            FROM {TABLE_NAME}
            WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()
        if not lead_row:
            return

        thread_subject = (lead_row["thread_subject"] or "").strip() or _normalize_thread_subject(subject)
        next_followup_at = _next_followup_at(now_iso, touch_number, resolved_settings)
        sequence_status = "active" if next_followup_at else "completed"
        sequence_completed_at = now_iso if sequence_status == "completed" else None
        first_sent_at = lead_row["sent_at"] or now_iso

        connection.execute(
            f"""
            INSERT INTO {EMAIL_EVENT_TABLE} (
                lead_id,
                touch_number,
                touch_type,
                subject,
                body_preview,
                scheduled_for,
                sent_at,
                status,
                message_id,
                in_reply_to,
                tracking_token,
                copy_quality_score,
                copy_quality_flags,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?, ?, ?, '')
            """,
            (
                lead_id,
                touch_number,
                touch_type,
                subject,
                _body_preview(body),
                scheduled_for,
                now_iso,
                message_id,
                in_reply_to,
                tracking_token,
                max(0, min(100, int(copy_quality_score or 0))),
                (copy_quality_flags or "")[:1000],
            ),
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET sent_at = ?,
                last_status = 'sent',
                last_error = '',
                last_contacted_at = ?,
                next_followup_at = ?,
                followup_count = ?,
                sequence_status = ?,
                sequence_completed_at = ?,
                thread_subject = ?,
                last_message_id = ?
            WHERE id = ?
            """,
            (
                first_sent_at,
                now_iso,
                next_followup_at,
                max(0, touch_number),
                sequence_status,
                sequence_completed_at,
                thread_subject,
                message_id,
                lead_id,
            ),
        )


def record_outreach_failure(
    lead_id: int,
    *,
    touch_type: str,
    touch_number: int,
    subject: str,
    error_message: str,
    scheduled_for: str | None = None,
) -> None:
    error_text = (error_message or "")[:2000]

    with get_connection() as connection:
        lead_row = connection.execute(
            f"SELECT sent_at, sequence_status FROM {TABLE_NAME} WHERE id = ?",
            (lead_id,),
        ).fetchone()
        if not lead_row:
            return

        sequence_status = (
            "pending"
            if lead_row["sent_at"] is None
            else (lead_row["sequence_status"] or "active")
        )

        connection.execute(
            f"""
            INSERT INTO {EMAIL_EVENT_TABLE} (
                lead_id,
                touch_number,
                touch_type,
                subject,
                body_preview,
                scheduled_for,
                sent_at,
                status,
                message_id,
                in_reply_to,
                error
            )
            VALUES (?, ?, ?, ?, '', ?, NULL, 'failed', '', '', ?)
            """,
            (
                lead_id,
                touch_number,
                touch_type,
                subject,
                scheduled_for,
                error_text,
            ),
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = 'failed',
                last_error = ?,
                sequence_status = ?
            WHERE id = ?
            """,
            (error_text, sequence_status, lead_id),
        )


def record_outreach_skip(
    lead_id: int,
    *,
    touch_type: str,
    touch_number: int,
    subject: str,
    reason: str,
    scheduled_for: str | None = None,
) -> None:
    reason_text = (reason or "")[:2000]

    with get_connection() as connection:
        lead_row = connection.execute(
            f"SELECT sent_at, sequence_status FROM {TABLE_NAME} WHERE id = ?",
            (lead_id,),
        ).fetchone()
        if not lead_row:
            return

        sequence_status = (
            "pending"
            if lead_row["sent_at"] is None
            else (lead_row["sequence_status"] or "active")
        )

        connection.execute(
            f"""
            INSERT INTO {EMAIL_EVENT_TABLE} (
                lead_id,
                touch_number,
                touch_type,
                subject,
                body_preview,
                scheduled_for,
                sent_at,
                status,
                message_id,
                in_reply_to,
                error
            )
            VALUES (?, ?, ?, ?, '', ?, NULL, 'skipped', '', '', ?)
            """,
            (
                lead_id,
                touch_number,
                touch_type,
                subject,
                scheduled_for,
                reason_text,
            ),
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = 'skipped',
                last_error = ?,
                sequence_status = ?
            WHERE id = ?
            """,
            (reason_text, sequence_status, lead_id),
        )


def update_lead_delivery(
    email: str,
    *,
    last_status: str,
    last_error: str = "",
    sent: bool = False,
) -> None:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return

    if sent:
        sent_at = _now_iso()
        query = (
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = ?,
                last_error = ?,
                sent_at = ?,
                last_contacted_at = ?
            WHERE lower(email) = lower(?)
            """
        )
        params = (last_status, (last_error or "")[:2000], sent_at, sent_at, normalized_email)
    else:
        query = (
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = ?,
                last_error = ?
            WHERE lower(email) = lower(?)
            """
        )
        params = (last_status, (last_error or "")[:2000], normalized_email)

    with get_connection() as connection:
        connection.execute(query, params)


def _normalize_lead_payload(lead_data: dict) -> dict:
    normalized = {}
    for field in LEAD_FIELDS:
        value = lead_data.get(field, "") if lead_data else ""
        normalized[field] = (str(value).strip() if value is not None else "")

    normalized["email"] = normalized["email"].lower()
    return normalized


def _bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clean_text(value, default: str = "", maximum: int = 255) -> str:
    text = str(default if value is None else value).strip()
    return text[:maximum]


def _normalize_url(value, *, allow_empty: bool = True) -> str:
    text = _clean_text(value, "", 1000)
    if not text:
        if allow_empty:
            return ""
        raise ValueError("URL is required.")

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL: {text}")
    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"
    return normalized.rstrip("/")


def _read_sender_profile(connection) -> dict:
    identity_defaults = get_sender_identity_defaults()
    base = {**DEFAULT_SENDER_PROFILE, **identity_defaults}
    row = connection.execute(
        f"""
        SELECT
            sender_name,
            agency_name,
            website_url,
            tracking_base_url,
            open_tracking_enabled,
            reply_sync_enabled,
            warmup_enabled,
            warmup_status,
            warmup_recipients,
            warmup_started_at,
            warmup_day_count,
            warmup_stable_since,
            daily_send_limit,
            daily_warmup_target,
            deliverability_floor,
            spam_guard_enabled,
            snov_workspace_url,
            last_reply_sync_at,
            last_deliverability_check_at,
            updated_at
        FROM {SENDER_PROFILE_TABLE}
        WHERE id = 1
        """
    ).fetchone()

    if row:
        base.update(
            {
                "sender_name": row["sender_name"] or base["sender_name"],
                "agency_name": row["agency_name"] or base["agency_name"],
                "website_url": row["website_url"] or "",
                "tracking_base_url": row["tracking_base_url"] or "",
                "open_tracking_enabled": bool(row["open_tracking_enabled"]),
                "reply_sync_enabled": bool(row["reply_sync_enabled"]),
                "warmup_enabled": bool(row["warmup_enabled"]),
                "warmup_status": row["warmup_status"] or base["warmup_status"],
                "warmup_recipients": row["warmup_recipients"] or "",
                "warmup_started_at": row["warmup_started_at"] or None,
                "warmup_day_count": int(row["warmup_day_count"] or 0),
                "warmup_stable_since": row["warmup_stable_since"] or None,
                "daily_send_limit": int(row["daily_send_limit"]),
                "daily_warmup_target": int(row["daily_warmup_target"]),
                "deliverability_floor": int(row["deliverability_floor"]),
                "spam_guard_enabled": bool(row["spam_guard_enabled"]),
                "snov_workspace_url": row["snov_workspace_url"] or "",
                "last_reply_sync_at": row["last_reply_sync_at"] or None,
                "last_deliverability_check_at": row["last_deliverability_check_at"] or None,
                "updated_at": row["updated_at"] or None,
            }
        )
    else:
        base.update(
            {
                "last_reply_sync_at": None,
                "last_deliverability_check_at": None,
                "updated_at": None,
            }
        )

    base["tracking_base_url"] = _normalize_url(base.get("tracking_base_url"), allow_empty=True) if base.get("tracking_base_url") else ""
    base["website_url"] = _normalize_url(base.get("website_url"), allow_empty=True) if base.get("website_url") else ""
    base["snov_workspace_url"] = _normalize_url(base.get("snov_workspace_url"), allow_empty=True) if base.get("snov_workspace_url") else ""
    return base


def _normalize_sender_profile_payload(payload: dict, current: dict) -> dict:
    base = {**current}
    normalized = {
        "sender_name": _clean_text(payload.get("sender_name", base["sender_name"]), base["sender_name"], 120),
        "agency_name": _clean_text(payload.get("agency_name", base["agency_name"]), base["agency_name"], 120),
        "website_url": _normalize_url(payload.get("website_url", base.get("website_url", "")), allow_empty=True),
        "tracking_base_url": _normalize_url(payload.get("tracking_base_url", base.get("tracking_base_url", "")), allow_empty=True),
        "open_tracking_enabled": _bool_value(payload.get("open_tracking_enabled", base["open_tracking_enabled"])),
        "reply_sync_enabled": _bool_value(payload.get("reply_sync_enabled", base["reply_sync_enabled"])),
        "warmup_enabled": _bool_value(payload.get("warmup_enabled", base["warmup_enabled"])),
        "warmup_status": _clean_text(payload.get("warmup_status", base["warmup_status"]), base["warmup_status"], 32).lower() or "not_started",
        "warmup_recipients": _clean_text(payload.get("warmup_recipients", base.get("warmup_recipients", "")), "", 2000).strip(),
        "daily_send_limit": _safe_int(payload.get("daily_send_limit", base["daily_send_limit"]), int(base["daily_send_limit"]), 1, 500),
        "daily_warmup_target": _safe_int(payload.get("daily_warmup_target", base["daily_warmup_target"]), int(base["daily_warmup_target"]), 1, 500),
        "deliverability_floor": _safe_int(payload.get("deliverability_floor", base["deliverability_floor"]), int(base["deliverability_floor"]), 50, 100),
        "spam_guard_enabled": _bool_value(payload.get("spam_guard_enabled", base["spam_guard_enabled"])),
        "snov_workspace_url": _normalize_url(payload.get("snov_workspace_url", base.get("snov_workspace_url", "")), allow_empty=True),
    }
    return normalized


def _rate(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def _sender_health(connection, profile: dict) -> dict:
    cutoff = (_now() - timedelta(days=14)).isoformat()
    recent_row = connection.execute(
        f"""
        SELECT
            SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            AVG(CASE WHEN status = 'sent' THEN copy_quality_score END) AS avg_copy_score
        FROM {EMAIL_EVENT_TABLE}
        WHERE COALESCE(sent_at, created_at) >= ?
        """,
        (cutoff,),
    ).fetchone()
    today_sent_row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM {EMAIL_EVENT_TABLE}
        WHERE status = 'sent'
          AND substr(COALESCE(sent_at, created_at), 1, 10) = ?
        """,
        (date.today().isoformat(),),
    ).fetchone()

    sent_count = int((recent_row["sent_count"] if recent_row else 0) or 0)
    failed_count = int((recent_row["failed_count"] if recent_row else 0) or 0)
    avg_copy_score = float((recent_row["avg_copy_score"] if recent_row else 100) or 100)
    today_sent = int((today_sent_row["count"] if today_sent_row else 0) or 0)

    score = 100
    notes = []

    if profile.get("warmup_enabled") and profile.get("warmup_status") not in {"ready", "stable"}:
        score -= 12
        notes.append("Mailbox warmup is still in progress.")
    if profile.get("open_tracking_enabled") and not profile.get("tracking_base_url"):
        score -= 18
        notes.append("Open tracking is enabled but tracking URL is missing.")
    if not profile.get("website_url"):
        score -= 6
        notes.append("Website URL is missing from sender profile.")
    if profile.get("reply_sync_enabled") and not profile.get("last_reply_sync_at"):
        score -= 8
        notes.append("Reply sync has not run yet.")
    elif profile.get("reply_sync_enabled"):
        last_sync = _parse_datetime(profile.get("last_reply_sync_at"))
        if last_sync and (_now() - last_sync) > timedelta(days=3):
            score -= 6
            notes.append("Reply sync is stale.")
    if sent_count and failed_count / sent_count > 0.08:
        score -= 20
        notes.append("Recent failure rate is above 8 percent.")
    if today_sent > int(profile.get("daily_send_limit") or 25):
        score -= 10
        notes.append("Today's send volume is above the sender limit.")
    if avg_copy_score < 85:
        score -= 10
        notes.append("Spam guard score is trending low.")

    score = max(0, min(100, score))
    status = "healthy"
    if profile.get("warmup_enabled") and profile.get("warmup_status") not in {"ready", "stable"}:
        status = "warming"
    if score < 70:
        status = "at_risk"
    if score < 50:
        status = "blocked"

    if not notes:
        notes.append("Sender profile looks stable for the last 14 days.")

    return {
        "health_status": status,
        "health_score": score,
        "health_notes": notes[:4],
        "today_sent": today_sent,
        "avg_copy_score": round(avg_copy_score, 1),
    }


def get_sender_profile() -> dict:
    with get_connection() as connection:
        profile = _read_sender_profile(connection)
        profile.update(_sender_health(connection, profile))
    return profile


def update_sender_profile(payload: dict) -> dict:
    with get_connection() as connection:
        current = _read_sender_profile(connection)
        profile = _normalize_sender_profile_payload(payload or {}, current)
        connection.execute(
            f"""
            UPDATE {SENDER_PROFILE_TABLE}
            SET sender_name = ?,
                agency_name = ?,
                website_url = ?,
                tracking_base_url = ?,
                open_tracking_enabled = ?,
                reply_sync_enabled = ?,
                warmup_enabled = ?,
                warmup_status = ?,
                warmup_recipients = ?,
                daily_send_limit = ?,
                daily_warmup_target = ?,
                deliverability_floor = ?,
                spam_guard_enabled = ?,
                snov_workspace_url = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                profile["sender_name"],
                profile["agency_name"],
                profile["website_url"],
                profile["tracking_base_url"],
                int(profile["open_tracking_enabled"]),
                int(profile["reply_sync_enabled"]),
                int(profile["warmup_enabled"]),
                profile["warmup_status"],
                profile["warmup_recipients"],
                profile["daily_send_limit"],
                profile["daily_warmup_target"],
                profile["deliverability_floor"],
                int(profile["spam_guard_enabled"]),
                profile["snov_workspace_url"],
                _now_iso(),
            ),
        )

        updated = _read_sender_profile(connection)
        updated.update(_sender_health(connection, updated))
    return updated


def record_reply_sync_completed() -> None:
    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE {SENDER_PROFILE_TABLE}
            SET last_reply_sync_at = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (_now_iso(), _now_iso()),
        )


def record_open_event(tracking_token: str, *, remote_addr: str = "", user_agent: str = "") -> dict | None:
    token = _clean_text(tracking_token, "", 255)
    if not token:
        return None

    now_iso = _now_iso()
    remote_addr_text = _clean_text(remote_addr, "", 120)
    user_agent_text = _clean_text(user_agent, "", 255)
    fingerprint = _clean_text(f"{remote_addr_text}|{user_agent_text}".lower(), "", 400)

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT e.id AS email_event_id, e.lead_id
            FROM {EMAIL_EVENT_TABLE} e
            WHERE e.tracking_token = ?
            LIMIT 1
            """,
            (token,),
        ).fetchone()
        if not row:
            return None

        connection.execute(
            f"""
            INSERT INTO {EMAIL_OPEN_TABLE} (
                lead_id,
                email_event_id,
                tracking_token,
                opened_at,
                remote_addr,
                user_agent,
                viewer_fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["lead_id"]),
                int(row["email_event_id"]),
                token,
                now_iso,
                remote_addr_text,
                user_agent_text,
                fingerprint,
            ),
        )
        connection.execute(
            f"""
            UPDATE {EMAIL_EVENT_TABLE}
            SET open_count = COALESCE(open_count, 0) + 1,
                first_open_at = COALESCE(first_open_at, ?),
                last_open_at = ?
            WHERE id = ?
            """,
            (now_iso, now_iso, int(row["email_event_id"])),
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET open_count = COALESCE(open_count, 0) + 1,
                first_open_at = COALESCE(first_open_at, ?),
                last_open_at = ?
            WHERE id = ?
            """,
            (now_iso, now_iso, int(row["lead_id"])),
        )

        event_snapshot = connection.execute(
            f"""
            SELECT open_count, first_open_at, last_open_at
            FROM {EMAIL_EVENT_TABLE}
            WHERE id = ?
            """,
            (int(row["email_event_id"]),),
        ).fetchone()

    return {
        "lead_id": int(row["lead_id"]),
        "email_event_id": int(row["email_event_id"]),
        "open_count": int((event_snapshot["open_count"] if event_snapshot else 0) or 0),
        "first_open_at": event_snapshot["first_open_at"] if event_snapshot else None,
        "last_open_at": event_snapshot["last_open_at"] if event_snapshot else None,
    }


def mark_reply_received(
    *,
    lead_id: int | None = None,
    email: str | None = None,
    reply_type: str = "unknown",
    reply_summary: str = "",
    received_at: str | None = None,
    reference_message_ids: list[str] | None = None,
    provider: str = "imap",
    provider_message_id: str = "",
    inbound_message_id: str = "",
    inbound_in_reply_to: str = "",
    from_email: str = "",
    to_email: str = "",
    raw_body: str = "",
    clean_body: str = "",
    match_type: str = "",
) -> dict | None:
    if lead_id is None and not email:
        return None

    normalized_email = _clean_text((email or "").lower(), "", 254)
    normalized_type = _clean_text(reply_type or "unknown", "unknown", 32).lower()
    summary = _body_preview(reply_summary or clean_body or "", limit=500)
    lead_status = _lead_status_for_reply_type(normalized_type)
    # reply_received_at and the reply event's occurrence time reflect when our
    # system processed the message, NOT the sender's (often skewed) Date header.
    # Using processing time keeps the timeline ordered correctly against sent
    # events and keeps lead + event timestamps consistent.
    now_iso = _now_iso()
    references = [ref.strip() for ref in (reference_message_ids or []) if str(ref or "").strip()]
    provider_value = _clean_text(provider or "imap", "imap", 32).lower()
    provider_message_id = _clean_text(provider_message_id or "", "", 254)
    inbound_message_id = _clean_text(inbound_message_id or "", "", 254)
    inbound_in_reply_to = _clean_text(inbound_in_reply_to or "", "", 254)
    from_email = _clean_text((from_email or normalized_email).lower(), "", 254)
    to_email = _clean_text((to_email or "").lower(), "", 254)
    raw_body_value = str(raw_body or "")[:5000]
    clean_body_value = str(clean_body or reply_summary or "")[:5000]
    match_type = _clean_text(match_type or "", "", 64)

    with get_connection() as connection:
        if lead_id is not None:
            lead_row = connection.execute(
                f"""
                SELECT id, email, thread_subject
                FROM {TABLE_NAME}
                WHERE id = ?
                """,
                (int(lead_id),),
            ).fetchone()
        else:
            lead_row = connection.execute(
                f"""
                SELECT id, email, thread_subject
                FROM {TABLE_NAME}
                WHERE lower(email) = lower(?)
                """,
                (normalized_email,),
            ).fetchone()

        if not lead_row:
            return None

        duplicate_clauses = []
        duplicate_params: list[str] = []
        if provider_message_id:
            duplicate_clauses.append("(provider = ? AND provider_message_id = ?)")
            duplicate_params.extend([provider_value, provider_message_id])
        if inbound_message_id:
            duplicate_clauses.append("message_id = ?")
            duplicate_params.append(inbound_message_id)
        if duplicate_clauses:
            duplicate_row = connection.execute(
                f"""
                SELECT id
                FROM {EMAIL_EVENT_TABLE}
                WHERE status = 'reply_received'
                  AND ({' OR '.join(duplicate_clauses)})
                LIMIT 1
                """,
                duplicate_params,
            ).fetchone()
            if duplicate_row:
                row = connection.execute(
                    f"""
                    SELECT {_lead_select_fields()}
                    FROM {TABLE_NAME}
                    WHERE id = ?
                    """,
                    (int(lead_row["id"]),),
                ).fetchone()
                result = row_to_dict(row)
                if result is not None:
                    result["_duplicate_reply"] = True
                return result

        resolved_lead_id = int(lead_row["id"])
        opt_out_at = now_iso if normalized_type in {"unsubscribe", "not_interested", "opt_out"} else None

        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET last_status = ?,
                last_error = '',
                reply_received_at = ?,
                reply_type = ?,
                reply_summary = ?,
                opt_out_at = COALESCE(opt_out_at, ?),
                next_followup_at = NULL,
                sequence_status = 'stopped',
                sequence_completed_at = COALESCE(sequence_completed_at, ?)
            WHERE id = ?
            """,
            (lead_status, now_iso, normalized_type, summary, opt_out_at, now_iso, resolved_lead_id),
        )

        if references:
            placeholders = ",".join("?" for _ in references)
            connection.execute(
                f"""
                UPDATE {EMAIL_EVENT_TABLE}
                SET reply_type = ?,
                    reply_summary = ?
                WHERE lead_id = ?
                  AND status = 'sent'
                  AND message_id IN ({placeholders})
                """,
                (normalized_type, summary, resolved_lead_id, *references),
            )
        else:
            latest = connection.execute(
                f"""
                SELECT id
                FROM {EMAIL_EVENT_TABLE}
                WHERE lead_id = ?
                  AND status = 'sent'
                ORDER BY id DESC
                LIMIT 1
                """,
                (resolved_lead_id,),
            ).fetchone()
            if latest:
                connection.execute(
                    f"""
                    UPDATE {EMAIL_EVENT_TABLE}
                    SET reply_type = ?,
                        reply_summary = ?
                    WHERE id = ?
                    """,
                    (normalized_type, summary, int(latest["id"])),
                )

        try:
            connection.execute(
                f"""
                INSERT INTO {EMAIL_EVENT_TABLE} (
                    lead_id,
                    touch_number,
                    touch_type,
                    subject,
                    body_preview,
                    scheduled_for,
                    sent_at,
                    status,
                    message_id,
                    in_reply_to,
                    tracking_token,
                    copy_quality_score,
                    copy_quality_flags,
                    reply_type,
                    reply_summary,
                    error,
                    provider,
                    provider_message_id,
                    from_email,
                    to_email,
                    raw_body,
                    clean_body,
                    match_type,
                    processed_at
                )
                VALUES (?, 0, 'reply', ?, ?, NULL, ?, 'reply_received', ?, ?, '', 100, '', ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_lead_id,
                    (lead_row["thread_subject"] or "").strip() or f"Reply from {lead_row['email']}",
                    summary,
                    now_iso,
                    inbound_message_id,
                    inbound_in_reply_to,
                    normalized_type,
                    summary,
                    provider_value,
                    provider_message_id,
                    from_email,
                    to_email,
                    raw_body_value,
                    clean_body_value,
                    match_type,
                    now_iso,
                ),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                f"""
                SELECT {_lead_select_fields()}
                FROM {TABLE_NAME}
                WHERE id = ?
                """,
                (resolved_lead_id,),
            ).fetchone()
            result = row_to_dict(row)
            if result is not None:
                result["_duplicate_reply"] = True
            return result

        row = connection.execute(
            f"""
            SELECT {_lead_select_fields()}
            FROM {TABLE_NAME}
            WHERE id = ?
            """,
            (resolved_lead_id,),
        ).fetchone()

    return row_to_dict(row)

def list_recent_activity(limit: int = 12) -> list[dict]:
    max_items = max(1, min(50, int(limit or 12)))
    activity = []

    with get_connection() as connection:
        event_rows = connection.execute(
            f"""
            SELECT
                e.status,
                e.touch_type,
                e.subject,
                e.body_preview,
                COALESCE(e.sent_at, e.created_at) AS occurred_at,
                l.name,
                l.email,
                l.company
            FROM {EMAIL_EVENT_TABLE} e
            JOIN {TABLE_NAME} l ON l.id = e.lead_id
            ORDER BY COALESCE(e.sent_at, e.created_at) DESC
            LIMIT ?
            """,
            (max_items,),
        ).fetchall()
        open_rows = connection.execute(
            f"""
            SELECT
                'open' AS status,
                'open' AS touch_type,
                '' AS subject,
                '' AS body_preview,
                opened_at AS occurred_at,
                l.name,
                l.email,
                l.company
            FROM {EMAIL_OPEN_TABLE} o
            JOIN {TABLE_NAME} l ON l.id = o.lead_id
            ORDER BY opened_at DESC
            LIMIT ?
            """,
            (max_items,),
        ).fetchall()

    for row in event_rows:
        activity.append(
            {
                "type": row["status"],
                "touch_type": row["touch_type"],
                "occurred_at": row["occurred_at"],
                "lead_name": row["name"] or row["email"],
                "email": row["email"],
                "company": row["company"] or "",
                "summary": row["subject"] or row["body_preview"] or row["status"],
            }
        )
    for row in open_rows:
        activity.append(
            {
                "type": "open",
                "touch_type": "open",
                "occurred_at": row["occurred_at"],
                "lead_name": row["name"] or row["email"],
                "email": row["email"],
                "company": row["company"] or "",
                "summary": "Email opened",
            }
        )

    activity.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return activity[:max_items]


def get_report_overview(days: int = 30) -> dict:
    window_days = max(1, min(365, int(days or 30)))
    cutoff_iso = (_now() - timedelta(days=window_days)).isoformat()

    with get_connection() as connection:
        totals = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_leads,
                SUM(CASE WHEN l.sent_at IS NULL THEN 1 ELSE 0 END) AS pending_leads,
                SUM(CASE WHEN l.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent_leads,
                SUM(CASE WHEN COALESCE(l.open_count, 0) > 0 THEN 1 ELSE 0 END) AS opened_leads,
                SUM(CASE WHEN l.reply_received_at IS NOT NULL OR EXISTS (
                    SELECT 1 FROM {EMAIL_EVENT_TABLE} e
                    WHERE e.lead_id = l.id
                      AND (e.status = 'reply_received' OR e.reply_type != '')
                ) THEN 1 ELSE 0 END) AS replied_leads,
                SUM(CASE WHEN lower(l.reply_type) IN ('interested', 'positive') OR EXISTS (
                    SELECT 1 FROM {EMAIL_EVENT_TABLE} e
                    WHERE e.lead_id = l.id
                      AND lower(e.reply_type) IN ('interested', 'positive')
                ) THEN 1 ELSE 0 END) AS positive_replies,
                SUM(CASE WHEN l.opt_out_at IS NOT NULL OR lower(l.reply_type) IN ('unsubscribe', 'not_interested', 'opt_out') THEN 1 ELSE 0 END) AS opt_outs,
                SUM(CASE WHEN l.last_status = 'failed' THEN 1 ELSE 0 END) AS failed_leads,
                SUM(CASE WHEN l.last_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_leads
            FROM {TABLE_NAME} l
            WHERE l.archived_at IS NULL
            """
        ).fetchone()
        recent_events = connection.execute(
            f"""
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_events,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_events,
                SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_events,
                AVG(CASE WHEN status = 'sent' THEN copy_quality_score END) AS avg_copy_score
            FROM {EMAIL_EVENT_TABLE}
            WHERE COALESCE(sent_at, created_at) >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
        recent_opens = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_opens,
                COUNT(DISTINCT CASE WHEN viewer_fingerprint != '' THEN viewer_fingerprint ELSE NULL END) AS unique_viewers,
                COUNT(DISTINCT lead_id) AS opened_leads
            FROM {EMAIL_OPEN_TABLE}
            WHERE opened_at >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
        recent_replies = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM {TABLE_NAME}
            WHERE reply_received_at IS NOT NULL
              AND reply_received_at >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
        flag_rows = connection.execute(
            f"""
            SELECT copy_quality_flags
            FROM {EMAIL_EVENT_TABLE}
            WHERE status = 'sent'
              AND copy_quality_flags != ''
              AND COALESCE(sent_at, created_at) >= ?
            ORDER BY COALESCE(sent_at, created_at) DESC
            LIMIT 100
            """,
            (cutoff_iso,),
        ).fetchall()
        top_rows = connection.execute(
            f"""
            SELECT
                name,
                email,
                company,
                open_count,
                last_open_at,
                reply_type,
                reply_received_at
            FROM {TABLE_NAME}
            WHERE sent_at IS NOT NULL
            ORDER BY
                CASE WHEN reply_received_at IS NOT NULL THEN 1 ELSE 0 END DESC,
                COALESCE(open_count, 0) DESC,
                COALESCE(last_open_at, '') DESC,
                id DESC
            LIMIT 5
            """
        ).fetchall()

        profile = _read_sender_profile(connection)
        health = _sender_health(connection, profile)

    totals_data = {
        "total_leads": int((totals["total_leads"] if totals else 0) or 0),
        "pending_leads": int((totals["pending_leads"] if totals else 0) or 0),
        "sent_leads": int((totals["sent_leads"] if totals else 0) or 0),
        "opened_leads": int((totals["opened_leads"] if totals else 0) or 0),
        "replied_leads": int((totals["replied_leads"] if totals else 0) or 0),
        "positive_replies": int((totals["positive_replies"] if totals else 0) or 0),
        "opt_outs": int((totals["opt_outs"] if totals else 0) or 0),
        "failed_leads": int((totals["failed_leads"] if totals else 0) or 0),
        "skipped_leads": int((totals["skipped_leads"] if totals else 0) or 0),
    }
    recent_data = {
        "sent_events": int((recent_events["sent_events"] if recent_events else 0) or 0),
        "failed_events": int((recent_events["failed_events"] if recent_events else 0) or 0),
        "skipped_events": int((recent_events["skipped_events"] if recent_events else 0) or 0),
        "avg_copy_score": round(float((recent_events["avg_copy_score"] if recent_events else 100) or 100), 1),
        "total_opens": int((recent_opens["total_opens"] if recent_opens else 0) or 0),
        "unique_viewers": int((recent_opens["unique_viewers"] if recent_opens else 0) or 0),
        "opened_leads": int((recent_opens["opened_leads"] if recent_opens else 0) or 0),
        "recent_replies": int((recent_replies["count"] if recent_replies else 0) or 0),
    }

    flag_counts = {}
    for row in flag_rows:
        for flag in [item.strip() for item in str(row["copy_quality_flags"] or "").split(",") if item.strip()]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    top_engaged = [
        {
            "name": row["name"] or row["email"],
            "email": row["email"],
            "company": row["company"] or "",
            "open_count": int(row["open_count"] or 0),
            "last_open_at": row["last_open_at"] or None,
            "reply_type": row["reply_type"] or "",
            "reply_received_at": row["reply_received_at"] or None,
        }
        for row in top_rows
    ]

    return {
        "window_days": window_days,
        "totals": totals_data,
        "recent": recent_data,
        "rates": {
            "open_rate": _rate(totals_data["opened_leads"], totals_data["sent_leads"]),
            "reply_rate": _rate(totals_data["replied_leads"], totals_data["sent_leads"]),
            "positive_reply_rate": _rate(totals_data["positive_replies"], totals_data["sent_leads"]),
            "failure_rate": _rate(totals_data["failed_leads"], totals_data["sent_leads"]),
        },
        "copy_health": {
            "avg_score": recent_data["avg_copy_score"],
            "common_flags": [
                {"flag": name, "count": count}
                for name, count in sorted(flag_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
            ],
        },
        "sender_health": health,
        "sender_profile": {
            "sender_name": profile["sender_name"],
            "agency_name": profile["agency_name"],
            "website_url": profile["website_url"],
            "tracking_base_url": profile["tracking_base_url"],
            "warmup_status": profile["warmup_status"],
            "warmup_recipients": profile.get("warmup_recipients", ""),
            "warmup_day_count": profile.get("warmup_day_count", 0),
            "daily_send_limit": profile["daily_send_limit"],
            "daily_warmup_target": profile["daily_warmup_target"],
            "deliverability_floor": profile["deliverability_floor"],
            "snov_workspace_url": profile["snov_workspace_url"],
            "last_reply_sync_at": profile.get("last_reply_sync_at"),
        },
        "top_engaged_leads": top_engaged,
        "recent_activity": list_recent_activity(limit=10),
    }
