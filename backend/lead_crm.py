"""Lead Workspace CRM - Timeline, notes, campaign history for each lead."""

import json
from datetime import datetime, timezone

from backend.app.database import (
    EMAIL_EVENT_TABLE,
    EMAIL_OPEN_TABLE,
    TABLE_NAME,
    get_connection,
    row_to_dict,
)
from backend.app.crud import _lead_status_for_reply_type

LEAD_NOTE_TABLE = "app_lead_note"
LEAD_TAG_TABLE = "app_lead_tag"
LEAD_ACTIVITY_TABLE = "app_lead_activity"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_crm_tables() -> None:
    with get_connection() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {LEAD_NOTE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                user_id INTEGER NULL,
                content TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lead_id) REFERENCES {TABLE_NAME}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {LEAD_TAG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                tag VARCHAR(100) NOT NULL,
                color VARCHAR(7) NOT NULL DEFAULT '#FF6B00',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lead_id) REFERENCES {TABLE_NAME}(id) ON DELETE CASCADE,
                UNIQUE(lead_id, tag)
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {LEAD_ACTIVITY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                activity_type VARCHAR(50) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{{}}',
                user_id INTEGER NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lead_id) REFERENCES {TABLE_NAME}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{LEAD_NOTE_TABLE}_lead ON {LEAD_NOTE_TABLE}(lead_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{LEAD_TAG_TABLE}_lead ON {LEAD_TAG_TABLE}(lead_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{LEAD_ACTIVITY_TABLE}_lead ON {LEAD_ACTIVITY_TABLE}(lead_id)")


def get_lead_detail(lead_id: int) -> dict | None:
    with get_connection() as conn:
        lead_row = conn.execute(
            f"""
            SELECT
                id, name, email, niche, industry, phone, company,
                sent_at, last_status, last_error, last_contacted_at,
                next_followup_at, followup_count, sequence_status,
                reply_received_at, opt_out_at, sequence_completed_at,
                thread_subject, last_message_id,
                first_open_at, last_open_at, open_count,
                reply_type, reply_summary, archived_at
            FROM {TABLE_NAME} WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()

        if not lead_row:
            return None

        lead = row_to_dict(lead_row) or {}

        notes = conn.execute(
            f"SELECT * FROM {LEAD_NOTE_TABLE} WHERE lead_id = ? ORDER BY created_at DESC",
            (lead_id,),
        ).fetchall()
        lead["notes"] = [row_to_dict(n) for n in notes]

        tags = conn.execute(
            f"SELECT * FROM {LEAD_TAG_TABLE} WHERE lead_id = ? ORDER BY tag",
            (lead_id,),
        ).fetchall()
        lead["tags"] = [row_to_dict(t) for t in tags]

        events = conn.execute(
            f"""
            SELECT id, touch_number, touch_type, subject, body_preview,
                   sent_at, status, tracking_token, open_count,
                   reply_type, reply_summary, error, created_at
            FROM {EMAIL_EVENT_TABLE}
            WHERE lead_id = ?
            ORDER BY created_at DESC
            """,
            (lead_id,),
        ).fetchall()
        lead["email_events"] = [row_to_dict(e) for e in events]

        opens = conn.execute(
            f"""
            SELECT opened_at, remote_addr, user_agent
            FROM {EMAIL_OPEN_TABLE}
            WHERE lead_id = ?
            ORDER BY opened_at DESC
            LIMIT 50
            """,
            (lead_id,),
        ).fetchall()
        lead["open_events"] = [row_to_dict(o) for o in opens]

        activities = conn.execute(
            f"""
            SELECT * FROM {LEAD_ACTIVITY_TABLE}
            WHERE lead_id = ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (lead_id,),
        ).fetchall()
        lead["activities"] = [row_to_dict(a) for a in activities]

    return lead


def add_lead_note(lead_id: int, content: str, user_id: int | None = None) -> dict:
    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO {LEAD_NOTE_TABLE} (lead_id, user_id, content)
            VALUES (?, ?, ?)
            """,
            (lead_id, user_id, content),
        )
        row = conn.execute(
            f"SELECT * FROM {LEAD_NOTE_TABLE} WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

        _log_lead_activity(conn, lead_id, "note_added", f"Note added: {content[:100]}", user_id)

    return row_to_dict(row) or {}


def update_lead_note(note_id: int, content: str) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            f"UPDATE {LEAD_NOTE_TABLE} SET content = ?, updated_at = ? WHERE id = ?",
            (content, _utcnow_iso(), note_id),
        )
        row = conn.execute(
            f"SELECT * FROM {LEAD_NOTE_TABLE} WHERE id = ?", (note_id,)
        ).fetchone()
    return row_to_dict(row) if row else None


def delete_lead_note(note_id: int) -> bool:
    with get_connection() as conn:
        conn.execute(f"DELETE FROM {LEAD_NOTE_TABLE} WHERE id = ?", (note_id,))
    return True


def add_lead_tag(lead_id: int, tag: str, color: str = "#FF6B00") -> dict:
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                f"""
                INSERT INTO {LEAD_TAG_TABLE} (lead_id, tag, color)
                VALUES (?, ?, ?)
                """,
                (lead_id, tag.strip(), color),
            )
            row = conn.execute(
                f"SELECT * FROM {LEAD_TAG_TABLE} WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return row_to_dict(row) or {}
        except Exception:
            existing = conn.execute(
                f"SELECT * FROM {LEAD_TAG_TABLE} WHERE lead_id = ? AND tag = ?",
                (lead_id, tag.strip()),
            ).fetchone()
            return row_to_dict(existing) if existing else {}


def remove_lead_tag(lead_id: int, tag: str) -> bool:
    with get_connection() as conn:
        conn.execute(
            f"DELETE FROM {LEAD_TAG_TABLE} WHERE lead_id = ? AND tag = ?",
            (lead_id, tag.strip()),
        )
    return True


def _timeline_event_label(subtype: str, status: str, reply_type: str = "") -> str:
    if status == "reply_received":
        labels = {
            "interested": "Reply Received (Interested)",
            "positive": "Reply Received (Positive)",
            "not_interested": "Reply Received (Not Interested)",
            "pricing_request": "Reply Received (Pricing Request)",
            "meeting_request": "Reply Received (Meeting Request)",
            "unsubscribe": "Reply Received (Unsubscribe)",
            "out_of_office": "Reply Received (Out of Office)",
            "auto_reply": "Auto Reply",
            "bounce": "Bounce",
            "question": "Reply Received (Question)",
        }
        return labels.get(reply_type, "Reply Received")
    if status == "failed":
        return "Email Failed"
    if subtype == "initial":
        return "Email Sent"
    if subtype == "followup_1":
        return "Follow-up 1 Sent"
    if subtype == "followup_2":
        return "Follow-up 2 Sent"
    if subtype == "final_bump":
        return "Final Bump Sent"
    if subtype == "open":
        return "Email Opened"
    if subtype == "note":
        return "Note Added"
    return "Email Sent"


def get_lead_timeline(lead_id: int) -> list[dict]:
    timeline = []

    with get_connection() as conn:
        lead_row = conn.execute(
            f"SELECT id, email, last_status, reply_type, reply_summary, reply_received_at FROM {TABLE_NAME} WHERE id = ?",
            (lead_id,),
        ).fetchone()

        events = conn.execute(
            f"""
            SELECT
                id,
                touch_type AS subtype,
                subject,
                body_preview,
                clean_body,
                COALESCE(sent_at, created_at) AS occurred_at,
                status,
                open_count,
                reply_type,
                reply_summary,
                error,
                from_email,
                match_type,
                message_id,
                provider_message_id
            FROM {EMAIL_EVENT_TABLE}
            WHERE lead_id = ?
            """,
            (lead_id,),
        ).fetchall()

        opens = conn.execute(
            f"""
            SELECT
                'open' AS subtype,
                user_agent AS description,
                opened_at AS occurred_at
            FROM {EMAIL_OPEN_TABLE}
            WHERE lead_id = ?
            """,
            (lead_id,),
        ).fetchall()

        notes = conn.execute(
            f"""
            SELECT
                'note' AS subtype,
                content AS description,
                created_at AS occurred_at
            FROM {LEAD_NOTE_TABLE}
            WHERE lead_id = ?
            """,
            (lead_id,),
        ).fetchall()

    current_lead_status = (lead_row["last_status"] if lead_row else None) or "pending"

    for row in events:
        subtype = row["subtype"]
        status = row["status"]
        rtype = row["reply_type"] or ""

        if status == "reply_received":
            event_type = "REPLY_RECEIVED"
            display_body = row["clean_body"] or row["reply_summary"] or row["body_preview"] or ""
            # The status snapshot for a reply event is the consolidated business
            # status derived from this exact reply, never the lead's current state.
            display_status = _lead_status_for_reply_type(rtype)
            label = _timeline_event_label(subtype, status, rtype)
        elif status == "failed":
            event_type = "EMAIL_FAILED"
            display_body = row["error"] or row["body_preview"] or ""
            display_status = "failed"
            label = "Email Failed"
        elif status == "sent":
            event_type = "EMAIL_SENT"
            display_body = row["body_preview"] or ""
            display_status = "sent"
            label = _timeline_event_label(subtype, status, rtype)
        else:
            event_type = "EMAIL_EVENT"
            display_body = row["body_preview"] or ""
            display_status = status
            label = _timeline_event_label(subtype, status, rtype)

        timeline.append({
            "id": row["id"],
            "event_type": event_type,
            "type": event_type,
            "subtype": subtype,
            "title": label,
            "body": display_body,
            "description": display_body,
            "subject": row["subject"],
            "occurred_at": row["occurred_at"],
            "created_at": row["occurred_at"],
            "status": display_status,
            "lead_status": current_lead_status,
            "reply_id": row["id"] if status == "reply_received" else None,
            "message_id": row["message_id"] or None,
            "thread_id": None,
            "provider_message_id": row["provider_message_id"] or None,
            "open_count": row["open_count"],
            "reply_type": rtype,
            "from_email": row["from_email"],
            "match_type": row["match_type"],
        })

    for row in opens:
        occurred = row["occurred_at"]
        timeline.append({
            "id": None,
            "event_type": "OPEN",
            "type": "open",
            "subtype": "open",
            "title": "Email Opened",
            "body": row["description"],
            "description": row["description"],
            "occurred_at": occurred,
            "created_at": occurred,
            "lead_status": current_lead_status,
            "reply_id": None,
            "message_id": None,
            "thread_id": None,
        })

    for row in notes:
        occurred = row["occurred_at"]
        timeline.append({
            "id": None,
            "event_type": "NOTE",
            "type": "note",
            "subtype": "note",
            "title": "Note Added",
            "body": row["description"],
            "description": row["description"],
            "occurred_at": occurred,
            "created_at": occurred,
            "lead_status": current_lead_status,
            "reply_id": None,
            "message_id": None,
            "thread_id": None,
        })

    timeline.sort(key=lambda x: x.get("occurred_at") or "", reverse=True)
    return timeline


def _log_lead_activity(conn, lead_id: int, activity_type: str, description: str, user_id: int | None = None) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEAD_ACTIVITY_TABLE} (lead_id, activity_type, description, user_id)
        VALUES (?, ?, ?, ?)
        """,
        (lead_id, activity_type, description, user_id),
    )


def calculate_lead_score(lead_id: int) -> dict:
    with get_connection() as conn:
        lead = conn.execute(
            f"""
            SELECT
                COALESCE(open_count, 0) AS open_count,
                COALESCE(followup_count, 0) AS followup_count,
                reply_received_at, opt_out_at, sent_at,
                last_status, reply_type
            FROM {TABLE_NAME} WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()

    if not lead:
        return {"score": 0, "factors": []}

    score = 50
    factors = []

    if lead["open_count"] > 0:
        score += min(20, lead["open_count"] * 5)
        factors.append(f"Opened {lead['open_count']} time(s)")

    if lead["reply_received_at"]:
        if lead["reply_type"] in ("interested", "positive"):
            score += 30
            factors.append("Positive reply")
        else:
            score += 10
            factors.append("Replied")

    if lead["opt_out_at"]:
        score -= 40
        factors.append("Opted out")

    if lead["followup_count"] > 2:
        score -= 10
        factors.append("Multiple follow-ups")

    score = max(0, min(100, score))

    return {
        "score": score,
        "factors": factors,
        "rating": "hot" if score >= 70 else "warm" if score >= 40 else "cold",
    }
