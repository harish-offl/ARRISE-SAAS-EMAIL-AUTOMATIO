"""Authentication API routes."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from backend.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
)
from backend.auth_models import (
    authenticate_user,
    create_invitation,
    create_user,
    get_activity_log,
    get_user_by_id,
    get_user_orgs,
    get_user_workspaces,
    get_workspace_members,
    has_permission,
    log_activity,
)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)
    full_name: str = Field(default="", max_length=150)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class InviteRequest(BaseModel):
    email: EmailStr
    workspace_id: int
    role: str = Field(default="viewer")


class UpdateProfileRequest(BaseModel):
    full_name: str | None = None
    avatar_url: str | None = None


def _get_user_from_token(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = get_user_by_id(payload.get("sub"))
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def get_current_user(authorization: str | None = Header(None)) -> dict:
    return _get_user_from_token(authorization)


@router.post("/signup", status_code=201)
def signup(payload: SignupRequest, request: Request):
    try:
        user = create_user(payload.email, payload.password, payload.full_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    access = create_access_token({"sub": user["id"]})
    refresh = create_refresh_token({"sub": user["id"]})

    log_activity(
        user["id"], "signup",
        ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": user,
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    user = authenticate_user(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access = create_access_token({"sub": user["id"]})
    refresh = create_refresh_token({"sub": user["id"]})

    log_activity(
        user["id"], "login",
        ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": user,
    }


@router.post("/refresh")
def refresh_token(payload: RefreshRequest):
    token_data = decode_token(payload.refresh_token)
    if not token_data or token_data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = get_user_by_id(token_data.get("sub"))
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="User not found or inactive")

    access = create_access_token({"sub": user["id"]})
    refresh = create_refresh_token({"sub": user["id"]})

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
    }


@router.get("/me")
def get_me(user: dict = Depends(get_current_user)):
    workspaces = get_user_workspaces(user["id"])
    orgs = get_user_orgs(user["id"])
    return {
        **user,
        "workspaces": workspaces,
        "organizations": orgs,
    }


@router.put("/me")
def update_me(payload: UpdateProfileRequest, user: dict = Depends(get_current_user)):
    from backend.app.database import get_connection

    updates = {}
    if payload.full_name is not None:
        updates["full_name"] = payload.full_name
    if payload.avatar_url is not None:
        updates["avatar_url"] = payload.avatar_url

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        with get_connection() as conn:
            conn.execute(
                f"UPDATE app_user SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*values, user["id"]),
            )

    return get_user_by_id(user["id"])


@router.get("/workspaces")
def list_workspaces(user: dict = Depends(get_current_user)):
    return get_user_workspaces(user["id"])


@router.get("/workspaces/{workspace_id}/members")
def list_members(workspace_id: int, user: dict = Depends(get_current_user)):
    if not has_permission(user["id"], workspace_id, "viewer"):
        raise HTTPException(status_code=403, detail="Access denied")
    return get_workspace_members(workspace_id)


@router.post("/invitations")
def send_invitation(payload: InviteRequest, user: dict = Depends(get_current_user)):
    if not has_permission(user["id"], payload.workspace_id, "admin"):
        raise HTTPException(status_code=403, detail="Admin role required to invite")

    if payload.role not in ("admin", "manager", "sales", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")

    invitation = create_invitation(payload.email, payload.workspace_id, payload.role, user["id"])

    log_activity(
        user["id"], "invite_sent",
        workspace_id=payload.workspace_id,
        resource_type="invitation",
        resource_id=invitation["id"],
        details=f"Invited {payload.email} as {payload.role}",
    )

    return invitation


@router.get("/activity")
def activity_log(
    workspace_id: int | None = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    if workspace_id and not has_permission(user["id"], workspace_id, "viewer"):
        raise HTTPException(status_code=403, detail="Access denied")

    if workspace_id:
        return get_activity_log(workspace_id, limit)
    return []
