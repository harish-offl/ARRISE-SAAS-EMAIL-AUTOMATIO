import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.env_utils import BASE_DIR, DATA_DIR, load_project_env


TABLE_NAME = "app_lead"
EMAIL_EVENT_TABLE = "app_lead_email_event"
EMAIL_OPEN_TABLE = "app_lead_email_open_event"
WARMUP_EVENT_TABLE = "app_warmup_event"
FOLLOWUP_SETTINGS_TABLE = "app_followup_settings"
SENDER_PROFILE_TABLE = "app_sender_profile"
EMAIL_VERIFICATION_TABLE = "app_email_verification"

DEFAULT_FOLLOWUP_SETTINGS = {
    "enabled": False,
    "followup_1_delay_days": 3,
    "followup_2_delay_days": 7,
    "final_bump_delay_days": 12,
    "followup_1_date": None,
    "followup_2_date": None,
    "final_bump_date": None,
}


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


DEFAULT_SENDER_PROFILE = {
    "sender_name": os.getenv("SENDER_NAME", "Your Name").strip() or "Your Name",
    "agency_name": os.getenv("AGENCY_NAME", "Your Company Name").strip() or "Your Company Name",
    "website_url": os.getenv("WEBSITE_URL", "").strip(),
    "tracking_base_url": os.getenv("TRACKING_BASE_URL", "").strip(),
    "open_tracking_enabled": _env_flag("OPEN_TRACKING_ENABLED", False),
    "reply_sync_enabled": _env_flag("REPLY_SYNC_ENABLED", True),
    "warmup_enabled": _env_flag("WARMUP_ENABLED", False),
    "warmup_status": (os.getenv("WARMUP_STATUS", "not_started").strip() or "not_started")[:32],
    "warmup_recipients": os.getenv("WARMUP_RECIPIENTS", "").strip(),
    "warmup_started_at": None,
    "warmup_day_count": 0,
    "warmup_stable_since": None,
    "daily_send_limit": max(1, int(os.getenv("DAILY_SEND_LIMIT", "25"))),
    "daily_warmup_target": max(1, int(os.getenv("DAILY_WARMUP_TARGET", "40"))),
    "deliverability_floor": max(50, min(100, int(os.getenv("DELIVERABILITY_FLOOR", "90")))),
    "spam_guard_enabled": _env_flag("SPAM_GUARD_ENABLED", True),
    "snov_workspace_url": os.getenv("SNOV_WORKSPACE_URL", "").strip(),
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path() -> Path:
    load_project_env()
    raw_db_name = os.getenv("DATABASE_URL", str(DATA_DIR / "leads.db"))
    if raw_db_name.startswith("sqlite:///"):
        raw_db_name = raw_db_name.replace("sqlite:///", "", 1)
    elif raw_db_name.startswith("sqlite://"):
        raw_db_name = raw_db_name.replace("sqlite://", "", 1)

    db_path = Path(raw_db_name)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    return db_path


DB_PATH = _resolve_db_path()


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False, factory=ManagedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if column_name not in _column_names(connection, table_name):
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def initialize_database() -> None:
    with get_connection() as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NULL,
                email VARCHAR(254) NOT NULL UNIQUE,
                niche VARCHAR(100) NULL,
                industry VARCHAR(100) NULL,
                phone VARCHAR(20) NULL,
                company VARCHAR(100) NULL,
                last_error TEXT NOT NULL DEFAULT '',
                last_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                sent_at DATETIME NULL
            )
            """
        )

        _ensure_column(connection, TABLE_NAME, "last_contacted_at", "last_contacted_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "next_followup_at", "next_followup_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "followup_count", "followup_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(
            connection,
            TABLE_NAME,
            "sequence_status",
            "sequence_status VARCHAR(20) NOT NULL DEFAULT 'pending'",
        )





        _ensure_column(connection, TABLE_NAME, "reply_received_at", "reply_received_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "opt_out_at", "opt_out_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "sequence_completed_at", "sequence_completed_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "thread_subject", "thread_subject TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, TABLE_NAME, "last_message_id", "last_message_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, TABLE_NAME, "first_open_at", "first_open_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "last_open_at", "last_open_at DATETIME NULL")
        _ensure_column(connection, TABLE_NAME, "open_count", "open_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, TABLE_NAME, "reply_type", "reply_type TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, TABLE_NAME, "reply_summary", "reply_summary TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, TABLE_NAME, "archived_at", "archived_at DATETIME NULL")

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EMAIL_EVENT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                touch_number INTEGER NOT NULL DEFAULT 0,
                touch_type VARCHAR(20) NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                body_preview TEXT NOT NULL DEFAULT '',
                scheduled_for DATETIME NULL,
                sent_at DATETIME NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                message_id TEXT NOT NULL DEFAULT '',
                in_reply_to TEXT NOT NULL DEFAULT '',
                tracking_token TEXT NOT NULL DEFAULT '',
                first_open_at DATETIME NULL,
                last_open_at DATETIME NULL,
                open_count INTEGER NOT NULL DEFAULT 0,
                copy_quality_score INTEGER NOT NULL DEFAULT 100,
                copy_quality_flags TEXT NOT NULL DEFAULT '',
                reply_type TEXT NOT NULL DEFAULT '',
                reply_summary TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (lead_id) REFERENCES {TABLE_NAME}(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(connection, EMAIL_EVENT_TABLE, "tracking_token", "tracking_token TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "first_open_at", "first_open_at DATETIME NULL")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "last_open_at", "last_open_at DATETIME NULL")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "open_count", "open_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "copy_quality_score", "copy_quality_score INTEGER NOT NULL DEFAULT 100")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "copy_quality_flags", "copy_quality_flags TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "reply_type", "reply_type TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "reply_summary", "reply_summary TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "provider", "provider TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "provider_message_id", "provider_message_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "from_email", "from_email TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "to_email", "to_email TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "raw_body", "raw_body TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "clean_body", "clean_body TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "match_type", "match_type TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, EMAIL_EVENT_TABLE, "processed_at", "processed_at DATETIME NULL")

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EMAIL_OPEN_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                email_event_id INTEGER NOT NULL,
                tracking_token TEXT NOT NULL DEFAULT '',
                opened_at DATETIME NOT NULL,
                remote_addr TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                viewer_fingerprint TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (lead_id) REFERENCES {TABLE_NAME}(id) ON DELETE CASCADE,
                FOREIGN KEY (email_event_id) REFERENCES {EMAIL_EVENT_TABLE}(id) ON DELETE CASCADE
            )
            """
        )

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {FOLLOWUP_SETTINGS_TABLE} (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                followup_1_delay_days INTEGER NOT NULL DEFAULT 3,
                followup_2_delay_days INTEGER NOT NULL DEFAULT 7,
                final_bump_delay_days INTEGER NOT NULL DEFAULT 12,
                followup_1_date TEXT NULL,
                followup_2_date TEXT NULL,
                final_bump_date TEXT NULL,
                updated_at DATETIME NULL
            )
            """
        )
        _ensure_column(connection, FOLLOWUP_SETTINGS_TABLE, "followup_1_date", "followup_1_date TEXT NULL")
        _ensure_column(connection, FOLLOWUP_SETTINGS_TABLE, "followup_2_date", "followup_2_date TEXT NULL")
        _ensure_column(connection, FOLLOWUP_SETTINGS_TABLE, "final_bump_date", "final_bump_date TEXT NULL")

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SENDER_PROFILE_TABLE} (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                sender_name TEXT NOT NULL DEFAULT '',
                agency_name TEXT NOT NULL DEFAULT '',
                website_url TEXT NOT NULL DEFAULT '',
                tracking_base_url TEXT NOT NULL DEFAULT '',
                open_tracking_enabled INTEGER NOT NULL DEFAULT 0,
                reply_sync_enabled INTEGER NOT NULL DEFAULT 1,
                warmup_enabled INTEGER NOT NULL DEFAULT 0,
                warmup_status TEXT NOT NULL DEFAULT 'not_started',
                daily_send_limit INTEGER NOT NULL DEFAULT 25,
                daily_warmup_target INTEGER NOT NULL DEFAULT 40,
                deliverability_floor INTEGER NOT NULL DEFAULT 90,
                spam_guard_enabled INTEGER NOT NULL DEFAULT 1,
                snov_workspace_url TEXT NOT NULL DEFAULT '',
                last_reply_sync_at DATETIME NULL,
                last_deliverability_check_at DATETIME NULL,
                updated_at DATETIME NULL
            )
            """
        )
        _ensure_column(connection, SENDER_PROFILE_TABLE, "warmup_recipients", "warmup_recipients TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, SENDER_PROFILE_TABLE, "warmup_started_at", "warmup_started_at DATETIME NULL")
        _ensure_column(connection, SENDER_PROFILE_TABLE, "warmup_day_count", "warmup_day_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, SENDER_PROFILE_TABLE, "warmup_stable_since", "warmup_stable_since DATETIME NULL")

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WARMUP_EVENT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                error TEXT NOT NULL DEFAULT '',
                sent_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{WARMUP_EVENT_TABLE}_sent_at ON {WARMUP_EVENT_TABLE}(sent_at)"
        )

        connection.execute(
            f"""
            INSERT INTO {FOLLOWUP_SETTINGS_TABLE} (
                id,
                enabled,
                followup_1_delay_days,
                followup_2_delay_days,
                final_bump_delay_days,
                followup_1_date,
                followup_2_date,
                final_bump_date,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                int(DEFAULT_FOLLOWUP_SETTINGS["enabled"]),
                DEFAULT_FOLLOWUP_SETTINGS["followup_1_delay_days"],
                DEFAULT_FOLLOWUP_SETTINGS["followup_2_delay_days"],
                DEFAULT_FOLLOWUP_SETTINGS["final_bump_delay_days"],
                DEFAULT_FOLLOWUP_SETTINGS["followup_1_date"],
                DEFAULT_FOLLOWUP_SETTINGS["followup_2_date"],
                DEFAULT_FOLLOWUP_SETTINGS["final_bump_date"],
                _utcnow_iso(),
            ),
        )
        connection.execute(
            f"""
            INSERT INTO {SENDER_PROFILE_TABLE} (
                id,
                sender_name,
                agency_name,
                website_url,
                tracking_base_url,
                open_tracking_enabled,
                reply_sync_enabled,
                warmup_enabled,
                warmup_status,
                daily_send_limit,
                daily_warmup_target,
                deliverability_floor,
                spam_guard_enabled,
                snov_workspace_url,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                DEFAULT_SENDER_PROFILE["sender_name"],
                DEFAULT_SENDER_PROFILE["agency_name"],
                DEFAULT_SENDER_PROFILE["website_url"],
                DEFAULT_SENDER_PROFILE["tracking_base_url"],
                int(DEFAULT_SENDER_PROFILE["open_tracking_enabled"]),
                int(DEFAULT_SENDER_PROFILE["reply_sync_enabled"]),
                int(DEFAULT_SENDER_PROFILE["warmup_enabled"]),
                DEFAULT_SENDER_PROFILE["warmup_status"],
                DEFAULT_SENDER_PROFILE["daily_send_limit"],
                DEFAULT_SENDER_PROFILE["daily_warmup_target"],
                DEFAULT_SENDER_PROFILE["deliverability_floor"],
                int(DEFAULT_SENDER_PROFILE["spam_guard_enabled"]),
                DEFAULT_SENDER_PROFILE["snov_workspace_url"],
                _utcnow_iso(),
            ),
        )

        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET last_contacted_at = sent_at
            WHERE sent_at IS NOT NULL
              AND last_contacted_at IS NULL
            """
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET sequence_status = CASE
                WHEN opt_out_at IS NOT NULL OR reply_received_at IS NOT NULL THEN 'stopped'
                WHEN sent_at IS NULL THEN 'pending'
                WHEN next_followup_at IS NOT NULL THEN 'active'
                ELSE 'completed'
            END
            WHERE sequence_status IS NULL
               OR trim(sequence_status) = ''
               OR (sequence_status = 'pending' AND sent_at IS NOT NULL)
            """
        )
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET sequence_completed_at = COALESCE(sequence_completed_at, sent_at)
            WHERE sent_at IS NOT NULL
              AND next_followup_at IS NULL
              AND sequence_status = 'completed'
            """
        )

        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_sent_at ON {TABLE_NAME}(sent_at)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_email_lower ON {TABLE_NAME}(email)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_next_followup_at ON {TABLE_NAME}(next_followup_at)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_sequence_status ON {TABLE_NAME}(sequence_status)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_lead_id ON {EMAIL_EVENT_TABLE}(lead_id)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_status ON {EMAIL_EVENT_TABLE}(status)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_sent_at ON {EMAIL_EVENT_TABLE}(sent_at)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_message_id ON {EMAIL_EVENT_TABLE}(message_id)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_provider_message_id "
            f"ON {EMAIL_EVENT_TABLE}(provider_message_id) WHERE provider_message_id != ''"
        )
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_reply_provider_message_id "
            f"ON {EMAIL_EVENT_TABLE}(provider, provider_message_id) "
            f"WHERE status = 'reply_received' AND provider_message_id != ''"
        )
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_reply_message_id "
            f"ON {EMAIL_EVENT_TABLE}(message_id) "
            f"WHERE status = 'reply_received' AND message_id != ''"
        )
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{EMAIL_EVENT_TABLE}_tracking_token "
            f"ON {EMAIL_EVENT_TABLE}(tracking_token) WHERE tracking_token != ''"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_OPEN_TABLE}_lead_id ON {EMAIL_OPEN_TABLE}(lead_id)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_OPEN_TABLE}_opened_at ON {EMAIL_OPEN_TABLE}(opened_at)"
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EMAIL_VERIFICATION_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                normalized_email TEXT,
                syntax_valid INTEGER,
                domain TEXT,
                domain_exists INTEGER,
                mx_configured INTEGER,
                provider TEXT,
                disposable INTEGER,
                role_account INTEGER,
                mailbox_existence TEXT,
                smtp_status TEXT,
                risk_level TEXT,
                risk_reasons TEXT,
                final_status TEXT,
                recommendation TEXT,
                evidence TEXT,
                checked_at TEXT,
                created_at TEXT
            )
            """
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EMAIL_VERIFICATION_TABLE}_email "
            f"ON {EMAIL_VERIFICATION_TABLE}(normalized_email)"
        )
