"""User, Organization, and Workspace models with multi-tenancy support."""

import sqlite3
from datetime import datetime, timezone

from backend.app.database import get_connection, row_to_dict

USER_TABLE = "app_user"
ORG_TABLE = "app_organization"
WORKSPACE_TABLE = "app_workspace"
MEMBER_TABLE = "app_workspace_member"
INVITE_TABLE = "app_user_invitation"
ACTIVITY_LOG_TABLE = "app_activity_log"
TWO_FA_TABLE = "app_two_factor_auth"

ROLES = ("owner", "admin", "manager", "sales", "viewer")
ROLE_HIERARCHY = {"owner": 5, "admin": 4, "manager": 3, "sales": 2, "viewer": 1}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_auth_tables() -> None:
    with get_connection() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {USER_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(254) NOT NULL UNIQUE,
                full_name VARCHAR(150) NOT NULL DEFAULT '',
                hashed_password TEXT NOT NULL,
                avatar_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                is_verified INTEGER NOT NULL DEFAULT 0,
                is_superuser INTEGER NOT NULL DEFAULT 0,
                google_id TEXT DEFAULT NULL,
                microsoft_id TEXT DEFAULT NULL,
                github_id TEXT DEFAULT NULL,
                two_factor_enabled INTEGER NOT NULL DEFAULT 0,
                two_factor_secret TEXT DEFAULT NULL,
                verification_token TEXT NOT NULL DEFAULT '',
                reset_token TEXT NOT NULL DEFAULT '',
                reset_token_expires DATETIME NULL,
                last_login_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {ORG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                slug VARCHAR(200) NOT NULL UNIQUE,
                logo_url TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                owner_id INTEGER NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                stripe_customer_id TEXT DEFAULT NULL,
                stripe_subscription_id TEXT DEFAULT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES {USER_TABLE}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {WORKSPACE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                slug VARCHAR(200) NOT NULL,
                org_id INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES {ORG_TABLE}(id) ON DELETE CASCADE,
                UNIQUE(slug, org_id)
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                workspace_id INTEGER NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'viewer',
                invited_by INTEGER NULL,
                joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES {USER_TABLE}(id) ON DELETE CASCADE,
                FOREIGN KEY (workspace_id) REFERENCES {WORKSPACE_TABLE}(id) ON DELETE CASCADE,
                FOREIGN KEY (invited_by) REFERENCES {USER_TABLE}(id) ON DELETE SET NULL,
                UNIQUE(user_id, workspace_id)
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {INVITE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(254) NOT NULL,
                workspace_id INTEGER NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'viewer',
                invited_by INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                accepted INTEGER NOT NULL DEFAULT 0,
                expires_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (workspace_id) REFERENCES {WORKSPACE_TABLE}(id) ON DELETE CASCADE,
                FOREIGN KEY (invited_by) REFERENCES {USER_TABLE}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {ACTIVITY_LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NULL,
                org_id INTEGER NULL,
                workspace_id INTEGER NULL,
                action VARCHAR(100) NOT NULL,
                resource_type VARCHAR(50) NOT NULL DEFAULT '',
                resource_id INTEGER NULL,
                details TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES {USER_TABLE}(id) ON DELETE SET NULL,
                FOREIGN KEY (org_id) REFERENCES {ORG_TABLE}(id) ON DELETE SET NULL,
                FOREIGN KEY (workspace_id) REFERENCES {WORKSPACE_TABLE}(id) ON DELETE SET NULL
            )
        """)

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TWO_FA_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                secret TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES {USER_TABLE}(id) ON DELETE CASCADE
            )
        """)

        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{USER_TABLE}_email ON {USER_TABLE}(email)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ORG_TABLE}_owner ON {ORG_TABLE}(owner_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{WORKSPACE_TABLE}_org ON {WORKSPACE_TABLE}(org_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{MEMBER_TABLE}_user ON {MEMBER_TABLE}(user_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{MEMBER_TABLE}_workspace ON {MEMBER_TABLE}(workspace_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ACTIVITY_LOG_TABLE}_user ON {ACTIVITY_LOG_TABLE}(user_id)")


def create_user(email: str, password: str, full_name: str = "") -> dict:
    from backend.auth import hash_password

    normalized_email = email.strip().lower()
    hashed = hash_password(password)

    with get_connection() as conn:
        existing = conn.execute(
            f"SELECT id FROM {USER_TABLE} WHERE email = ?", (normalized_email,)
        ).fetchone()
        if existing:
            raise ValueError("Email already registered.")

        cursor = conn.execute(
            f"""
            INSERT INTO {USER_TABLE} (email, full_name, hashed_password)
            VALUES (?, ?, ?)
            """,
            (normalized_email, full_name.strip(), hashed),
        )
        user_id = cursor.lastrowid

        slug = normalized_email.split("@")[0].replace(".", "-").replace("_", "-")
        org_cursor = conn.execute(
            f"""
            INSERT INTO {ORG_TABLE} (name, slug, owner_id)
            VALUES (?, ?, ?)
            """,
            (full_name.strip() or normalized_email, slug, user_id),
        )
        org_id = org_cursor.lastrowid

        ws_cursor = conn.execute(
            f"""
            INSERT INTO {WORKSPACE_TABLE} (name, slug, org_id, is_default)
            VALUES (?, ?, ?, 1)
            """,
            ("Default Workspace", "default", org_id),
        )
        workspace_id = ws_cursor.lastrowid

        conn.execute(
            f"""
            INSERT INTO {MEMBER_TABLE} (user_id, workspace_id, role)
            VALUES (?, ?, 'owner')
            """,
            (user_id, workspace_id),
        )

        user_row = conn.execute(
            f"SELECT id, email, full_name, is_active, is_verified, created_at FROM {USER_TABLE} WHERE id = ?",
            (user_id,),
        ).fetchone()

    return row_to_dict(user_row) or {}


def authenticate_user(email: str, password: str) -> dict | None:
    from backend.auth import verify_password

    normalized_email = email.strip().lower()
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT id, email, full_name, hashed_password, is_active, is_verified, avatar_url
            FROM {USER_TABLE}
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()

    if not row:
        return None
    if not verify_password(password, row["hashed_password"]):
        return None
    if not row["is_active"]:
        return None

    with get_connection() as conn:
        conn.execute(
            f"UPDATE {USER_TABLE} SET last_login_at = ? WHERE id = ?",
            (_utcnow_iso(), row["id"]),
        )

    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "is_verified": bool(row["is_verified"]),
        "avatar_url": row["avatar_url"],
    }


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT id, email, full_name, is_active, is_verified, avatar_url,
                   two_factor_enabled, created_at, last_login_at
            FROM {USER_TABLE} WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def get_user_workspaces(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT w.id, w.name, w.slug, w.org_id, w.description, w.is_default,
                   m.role, o.name AS org_name, o.slug AS org_slug
            FROM {WORKSPACE_TABLE} w
            JOIN {MEMBER_TABLE} m ON m.workspace_id = w.id
            JOIN {ORG_TABLE} o ON o.id = w.org_id
            WHERE m.user_id = ?
            ORDER BY w.is_default DESC, w.name
            """,
            (user_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_user_orgs(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT o.id, o.name, o.slug, o.logo_url, o.plan, o.owner_id
            FROM {ORG_TABLE} o
            JOIN {WORKSPACE_TABLE} w ON w.org_id = o.id
            JOIN {MEMBER_TABLE} m ON m.workspace_id = w.id
            WHERE m.user_id = ?
            """,
            (user_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_workspace_members(workspace_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT u.id, u.email, u.full_name, u.avatar_url, m.role, m.joined_at
            FROM {MEMBER_TABLE} m
            JOIN {USER_TABLE} u ON u.id = m.user_id
            WHERE m.workspace_id = ?
            ORDER BY m.joined_at
            """,
            (workspace_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def log_activity(
    user_id: int | None,
    action: str,
    *,
    org_id: int | None = None,
    workspace_id: int | None = None,
    resource_type: str = "",
    resource_id: int | None = None,
    details: str = "",
    ip_address: str = "",
    user_agent: str = "",
) -> None:
    with get_connection() as conn:
        conn.execute(
            f"""
            INSERT INTO {ACTIVITY_LOG_TABLE}
            (user_id, org_id, workspace_id, action, resource_type, resource_id, details, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, org_id, workspace_id, action, resource_type, resource_id, details, ip_address, user_agent),
        )


def get_activity_log(workspace_id: int, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.*, u.full_name AS user_name, u.email AS user_email
            FROM {ACTIVITY_LOG_TABLE} a
            LEFT JOIN {USER_TABLE} u ON u.id = a.user_id
            WHERE a.workspace_id = ? OR a.workspace_id IS NULL
            ORDER BY a.created_at DESC
            LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def has_permission(user_id: int, workspace_id: int, required_role: str) -> bool:
    if ROLE_HIERARCHY.get(required_role, 0) <= 1:
        return True
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT role FROM {MEMBER_TABLE} WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ).fetchone()
    if not row:
        return False
    user_level = ROLE_HIERARCHY.get(row["role"], 0)
    return user_level >= ROLE_HIERARCHY.get(required_role, 0)


def create_invitation(email: str, workspace_id: int, role: str, invited_by: int) -> dict:
    import secrets
    from datetime import timedelta

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO {INVITE_TABLE} (email, workspace_id, role, invited_by, token, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (email.strip().lower(), workspace_id, role, invited_by, token, expires_at),
        )
        return {"id": cursor.lastrowid, "token": token, "expires_at": expires_at}
