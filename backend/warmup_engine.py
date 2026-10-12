"""Warm-up engine: ramps daily sending volume to build sender reputation.

The engine is intentionally independent of the cold-outreach campaign. It sends
low-volume, low-risk replies-to-self / friendly-address messages so the mailbox
gradually builds credibility before heavy outreach begins.

Warm-up ramping curve (per sender, per day):
    Day 1-3     5  emails
    Day 4-7     10 emails
    Day 8-14    20 emails
    Day 15-21   30 emails
    Day 22+     daily_warmup_target (capped)
"""

import re
import time
from datetime import date, datetime, timedelta, timezone

from backend.app.crud import _read_sender_profile, get_connection
from backend.app.database import SENDER_PROFILE_TABLE, WARMUP_EVENT_TABLE
from backend.config import DELAY_BETWEEN_EMAILS
from backend.smtp_sender import SMTPSender

WARMUP_STATUSES = {"not_started", "warming", "ready", "stable"}

_STABLE_DAYS_REQUIRED = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def day_count_for(profile: dict) -> int:
    started = _as_datetime(profile.get("warmup_started_at"))
    if not started:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, (date.today() - started.date()).days + 1)


def schedule_for_day(day_count: int, daily_target: int) -> int:
    """Return how many warm-up emails should be sent on a given warm-up day."""
    daily_target = max(1, int(daily_target))
    if day_count <= 0:
        return 0
    if day_count <= 3:
        target = 5
    elif day_count <= 7:
        target = 10
    elif day_count <= 14:
        target = 20
    elif day_count <= 21:
        target = 30
    else:
        target = daily_target
    return max(1, min(target, daily_target))


def parse_recipients(raw: str) -> list[str]:
    """Normalize a comma / space separated list of warm-up recipient addresses."""
    if not raw:
        return []
    emails = [e.strip().lower() for e in re.split(r"[,;\s]+", raw) if e.strip()]
    seen = set()
    out = []
    for e in emails:
        if "@" in e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def build_warmup_body(recipient_email: str, day_count: int) -> tuple[str, str]:
    """Generate a natural, low-risk warm-up email. Returns (subject, body)."""
    subject = f"Quick check-in - day {max(1, day_count)}"
    body = (
        f"Hi,\n\n"
        f"Just a quick check-in. This is automated warm-up message {max(1, day_count)} "
        f"from a fresh sending account to establish reliable inbox placement.\n\n"
        f"No action needed — simply archive or reply with \"stop\" if you'd like to opt out.\n\n"
        f"Thanks for indulging this brief reliability test.\n\n"
        f"Best regards"
    )
    return subject, body


def _insert_tracked_event(connection, recipient_email: str, subject: str, message_id: str) -> None:
    connection.execute(
        f"""
        INSERT INTO {WARMUP_EVENT_TABLE} (
            recipient_email, subject, message_id, status, sent_at
        )
        VALUES (?, ?, ?, 'sent', ?)
        """,
        (recipient_email, subject, message_id, _utcnow().isoformat()),
    )


def _count_sent_today(connection) -> int:
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM {WARMUP_EVENT_TABLE}
        WHERE status = 'sent'
          AND substr(COALESCE(sent_at, created_at), 1, 10) = ?
        """,
        (date.today().isoformat(),),
    ).fetchone()
    return int((row["count"] if row else 0) or 0)


def _update_status(connection, profile: dict) -> str:
    day_count = day_count_for(profile)
    daily_target = int(profile.get("daily_warmup_target") or 40)
    schedule_target = schedule_for_day(day_count, daily_target)
    stable_since = _as_datetime(profile.get("warmup_stable_since"))
    status = profile.get("warmup_status") or "not_started"

    if not profile.get("warmup_enabled"):
        new_status = "not_started"
    elif schedule_target >= daily_target:
        new_status = "stable"
    elif day_count >= 21:
        new_status = "ready"
    else:
        new_status = "warming"

    if new_status == "stable" and status != "stable":
        stable_since = _utcnow().isoformat()
    elif new_status != "stable":
        stable_since = None

    if new_status != status or (stable_since != profile.get("warmup_stable_since")):
        connection.execute(
            f"""
            UPDATE {SENDER_PROFILE_TABLE}
            SET warmup_status = ?, warmup_stable_since = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_status, stable_since, _utcnow().isoformat()),
        )
    return new_status


def update_status_snapshot() -> dict:
    """Refresh the computed warm-up status from the stored profile (no sends)."""
    with get_connection() as connection:
        profile = _read_sender_profile(connection)
        _update_status(connection, profile)
    return get_warmup_state()


def get_warmup_state() -> dict:
    with get_connection() as connection:
        profile = _read_sender_profile(connection)
        day_count = day_count_for(profile)
        daily_target = int(profile.get("daily_warmup_target") or 40)
        recipients = parse_recipients(profile.get("warmup_recipients", ""))
        sent_today = _count_sent_today(connection)
    return {
        "enabled": bool(profile.get("warmup_enabled")),
        "status": profile.get("warmup_status") or "not_started",
        "day_count": day_count,
        "daily_target": daily_target,
        "today_target": schedule_for_day(day_count, daily_target),
        "sent_today": sent_today,
        "recipients": recipients,
        "recipient_count": len(recipients),
    }


def start_warmup() -> dict:
    with get_connection() as connection:
        profile = _read_sender_profile(connection)
        if not profile.get("warmup_enabled"):
            raise RuntimeError("Warm-up is disabled. Enable 'Warmup mode' in Settings first.")
        recipients = parse_recipients(profile.get("warmup_recipients", ""))
        if not recipients:
            raise RuntimeError("No warm-up recipients configured. Add at least one recipient in Settings.")
        if not profile.get("warmup_started_at"):
            connection.execute(
                f"""
                UPDATE {SENDER_PROFILE_TABLE}
                SET warmup_started_at = ?, warmup_status = 'warming', warmup_day_count = 1, updated_at = ?
                WHERE id = 1
                """,
                (_utcnow().isoformat(), _utcnow().isoformat()),
            )
        profile = _read_sender_profile(connection)
        _update_status(connection, profile)
    return get_warmup_state()


def stop_warmup() -> dict:
    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE {SENDER_PROFILE_TABLE}
            SET warmup_status = 'not_started',
                warmup_started_at = NULL,
                warmup_day_count = 0,
                warmup_stable_since = NULL,
                updated_at = ?
            WHERE id = 1
            """,
            (_utcnow().isoformat(),),
        )
    return get_warmup_state()


def run_warmup_once(cap: int | None = None) -> dict:
    """Send today's warm-up emails. Returns a summary dict."""
    with get_connection() as connection:
        profile = _read_sender_profile(connection)

        if not profile.get("warmup_enabled"):
            return {"sent": 0, "skipped": True, "reason": "Warm-up disabled"}

        recipients = parse_recipients(profile.get("warmup_recipients", ""))
        if not recipients:
            return {"sent": 0, "skipped": True, "reason": "No warm-up recipients"}

        day_count = day_count_for(profile)
        today_target = schedule_for_day(day_count, int(profile.get("daily_warmup_target") or 40))
        already_sent = _count_sent_today(connection)
        remaining = max(0, today_target - already_sent)
        if cap is not None:
            remaining = max(0, min(remaining, cap))

        remaining_recipients = list(recipients)
        if not remaining:
            _update_status(connection, profile)
            return {
                "sent": 0,
                "already_sent": already_sent,
                "target": today_target,
                "day_count": day_count,
            }

    sent = 0
    errors = []
    with SMTPSender() as sender:
        for i in range(remaining):
            recipient = remaining_recipients[i % len(remaining_recipients)]
            subject, body = build_warmup_body(recipient, day_count)
            try:
                message_id = sender.send(recipient, subject, body, profile.get("sender_name", ""), profile.get("agency_name", ""))
                with get_connection() as connection:
                    _insert_tracked_event(connection, recipient, subject, message_id)
                sent += 1
                print(f"[warmup] Sent warm-up email to {recipient} (day {day_count}, {i+1}/{remaining})")
            except Exception as exc:
                errors.append(str(exc))
                print(f"[warmup] Failed to send to {recipient}: {exc}")
            if i < remaining - 1:
                time.sleep(max(DELAY_BETWEEN_EMAILS, 1))

    with get_connection() as connection:
        profile = _read_sender_profile(connection)
        _update_status(connection, profile)

    return {
        "sent": sent,
        "target": today_target,
        "day_count": day_count,
        "errors": errors,
        "error_count": len(errors),
    }
