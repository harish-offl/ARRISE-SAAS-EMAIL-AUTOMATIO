"""Dashboard analytics and reporting endpoints."""

from datetime import date, datetime, timedelta, timezone

from backend.app.database import (
    EMAIL_EVENT_TABLE,
    EMAIL_OPEN_TABLE,
    FOLLOWUP_SETTINGS_TABLE,
    SENDER_PROFILE_TABLE,
    TABLE_NAME,
    get_connection,
    row_to_dict,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def get_dashboard_stats() -> dict:
    with get_connection() as conn:
        totals = conn.execute(f"""
            SELECT
                COUNT(*) AS total_leads,
                SUM(CASE WHEN l.sent_at IS NULL AND l.last_status = 'pending' THEN 1 ELSE 0 END) AS pending_leads,
                SUM(CASE WHEN l.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent_leads,
                SUM(CASE WHEN l.reply_received_at IS NOT NULL OR EXISTS (
                    SELECT 1 FROM {EMAIL_EVENT_TABLE} e
                    WHERE e.lead_id = l.id
                      AND (e.status = 'reply_received' OR e.reply_type != '')
                ) THEN 1 ELSE 0 END) AS replied_leads,
                SUM(CASE WHEN l.last_status = 'failed' THEN 1 ELSE 0 END) AS failed_leads,
                SUM(CASE WHEN l.last_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_leads,
                SUM(CASE WHEN COALESCE(l.open_count, 0) > 0 THEN 1 ELSE 0 END) AS opened_leads,
                SUM(CASE WHEN lower(l.reply_type) IN ('interested', 'positive') OR EXISTS (
                    SELECT 1 FROM {EMAIL_EVENT_TABLE} e
                    WHERE e.lead_id = l.id
                      AND lower(e.reply_type) IN ('interested', 'positive')
                ) THEN 1 ELSE 0 END) AS positive_replies,
                SUM(CASE WHEN l.opt_out_at IS NOT NULL THEN 1 ELSE 0 END) AS opt_outs
            FROM {TABLE_NAME} l
            WHERE l.archived_at IS NULL
        """).fetchone()

        today = date.today().isoformat()
        today_events = conn.execute(f"""
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS emails_sent_today,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS emails_failed_today
            FROM {EMAIL_EVENT_TABLE}
            WHERE substr(COALESCE(sent_at, created_at), 1, 10) = ?
        """, (today,)).fetchone()

        today_opens = conn.execute(f"""
            SELECT COUNT(DISTINCT lead_id) AS unique_opens_today
            FROM {EMAIL_OPEN_TABLE}
            WHERE substr(opened_at, 1, 10) = ?
        """, (today,)).fetchone()

        profile = conn.execute(f"""
            SELECT daily_send_limit, warmup_status, warmup_enabled
            FROM {SENDER_PROFILE_TABLE} WHERE id = 1
        """).fetchone()

    total = int(totals["total_leads"] or 0)
    sent = int(totals["sent_leads"] or 0)
    opened = int(totals["opened_leads"] or 0)
    replied = int(totals["replied_leads"] or 0)
    positive = int(totals["positive_replies"] or 0)
    pending = int(totals["pending_leads"] or 0)
    failed = int(totals["failed_leads"] or 0)

    open_rate = round((opened / sent * 100), 1) if sent > 0 else 0.0
    reply_rate = round((replied / sent * 100), 1) if sent > 0 else 0.0
    positive_rate = round((positive / sent * 100), 1) if sent > 0 else 0.0
    bounce_rate = round((failed / sent * 100), 1) if sent > 0 else 0.0

    daily_limit = int(profile["daily_send_limit"] or 25) if profile else 25
    sent_today = int(today_events["emails_sent_today"] or 0) if today_events else 0
    remaining_today = max(0, daily_limit - sent_today)

    return {
        "total_leads": total,
        "pending_leads": pending,
        "sent_leads": sent,
        "replied_leads": replied,
        "failed_leads": failed,
        "skipped_leads": int(totals["skipped_leads"] or 0),
        "opened_leads": opened,
        "positive_replies": positive,
        "opt_outs": int(totals["opt_outs"] or 0),
        "open_rate": open_rate,
        "reply_rate": reply_rate,
        "positive_rate": positive_rate,
        "bounce_rate": bounce_rate,
        "emails_sent_today": sent_today,
        "emails_failed_today": int(today_events["emails_failed_today"] or 0) if today_events else 0,
        "unique_opens_today": int(today_opens["unique_opens_today"] or 0) if today_opens else 0,
        "daily_send_limit": daily_limit,
        "remaining_today": remaining_today,
        "warmup_status": profile["warmup_status"] if profile else "not_started",
        "warmup_enabled": bool(profile["warmup_enabled"]) if profile else False,
    }


def get_campaign_funnel() -> dict:
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT
                last_status,
                COUNT(*) AS count
            FROM {TABLE_NAME}
            WHERE archived_at IS NULL
            GROUP BY last_status
        """).fetchall()

    funnel = {}
    for row in rows:
        funnel[row["last_status"]] = int(row["count"] or 0)

    total = sum(funnel.values())
    return {
        "stages": [
            {"name": "Total Leads", "count": total, "color": "#FF6B00"},
            {"name": "Pending", "count": funnel.get("pending", 0), "color": "#FF6B00"},
            {"name": "Sent", "count": funnel.get("sent", 0), "color": "#FF6B00"},
            {"name": "Opened", "count": funnel.get("replied", 0) + funnel.get("sent", 0) - funnel.get("pending", 0), "color": "#FF6B00"},
            {"name": "Replied", "count": funnel.get("replied", 0), "color": "#FF6B00"},
            {"name": "Failed", "count": funnel.get("failed", 0), "color": "#FF6B00"},
        ],
        "total": total,
    }


def get_daily_stats(days: int = 30) -> list[dict]:
    window = max(1, min(365, days))
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT
                substr(COALESCE(sent_at, created_at), 1, 10) AS day,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM {EMAIL_EVENT_TABLE}
            WHERE COALESCE(sent_at, created_at) >= date('now', '-{window} days')
            GROUP BY day
            ORDER BY day
        """).fetchall()

    return [row_to_dict(r) for r in rows]


def get_top_campaigns(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT
                l.industry,
                COUNT(DISTINCT l.id) AS lead_count,
                SUM(CASE WHEN e.status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                SUM(CASE WHEN COALESCE(e.open_count, 0) > 0 THEN 1 ELSE 0 END) AS opened_count,
                SUM(CASE WHEN e.reply_type IN ('interested', 'positive') THEN 1 ELSE 0 END) AS positive_count
            FROM {TABLE_NAME} l
            LEFT JOIN {EMAIL_EVENT_TABLE} e ON e.lead_id = l.id
            WHERE l.archived_at IS NULL
              AND l.industry IS NOT NULL AND l.industry != ''
            GROUP BY l.industry
            ORDER BY lead_count DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [row_to_dict(r) for r in rows]


def get_smtp_health() -> dict:
    with get_connection() as conn:
        recent = conn.execute(f"""
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS total_sent,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS total_failed,
                SUM(CASE WHEN error LIKE '%timeout%' OR error LIKE '%connection%' THEN 1 ELSE 0 END) AS connection_errors,
                SUM(CASE WHEN error LIKE '%auth%' OR error LIKE '%535%' THEN 1 ELSE 0 END) AS auth_errors
            FROM {EMAIL_EVENT_TABLE}
            WHERE COALESCE(sent_at, created_at) >= date('now', '-7 days')
        """).fetchone()

    total_sent = int(recent["total_sent"] or 0) if recent else 0
    total_failed = int(recent["total_failed"] or 0) if recent else 0
    connection_errors = int(recent["connection_errors"] or 0) if recent else 0
    auth_errors = int(recent["auth_errors"] or 0) if recent else 0

    success_rate = round((total_sent / (total_sent + total_failed) * 100), 1) if (total_sent + total_failed) > 0 else 100.0

    health_status = "healthy"
    if success_rate < 90:
        health_status = "degraded"
    if success_rate < 70:
        health_status = "critical"

    return {
        "health_status": health_status,
        "success_rate_7d": success_rate,
        "total_sent_7d": total_sent,
        "total_failed_7d": total_failed,
        "connection_errors_7d": connection_errors,
        "auth_errors_7d": auth_errors,
    }


def get_notifications(user_id: int, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        recent_events = conn.execute(f"""
            SELECT
                'reply' AS type,
                l.name AS lead_name,
                l.email,
                e.reply_type,
                e.reply_summary,
                e.sent_at AS occurred_at
            FROM {EMAIL_EVENT_TABLE} e
            JOIN {TABLE_NAME} l ON l.id = e.lead_id
            WHERE e.reply_type != '' AND e.reply_type IS NOT NULL
            ORDER BY e.sent_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        recent_opens = conn.execute(f"""
            SELECT
                'open' AS type,
                l.name AS lead_name,
                l.email,
                o.opened_at AS occurred_at
            FROM {EMAIL_OPEN_TABLE} o
            JOIN {TABLE_NAME} l ON l.id = o.lead_id
            ORDER BY o.opened_at DESC
            LIMIT ?
        """, (limit // 2,)).fetchall()

    notifications = []
    for row in recent_events:
        notifications.append({
            "type": "reply",
            "title": f"Reply from {row['lead_name'] or row['email']}",
            "message": row["reply_summary"] or f"Type: {row['reply_type']}",
            "occurred_at": row["occurred_at"],
        })
    for row in recent_opens:
        notifications.append({
            "type": "open",
            "title": f"Email opened by {row['lead_name'] or row['email']}",
            "message": "Your email was opened",
            "occurred_at": row["occurred_at"],
        })

    notifications.sort(key=lambda x: x.get("occurred_at") or "", reverse=True)
    return notifications[:limit]
