"""FastAPI application and API routes for the email automation project."""

import csv
import io
import json
import logging
import re
import threading

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.app.campaign_status import (
    campaign_is_running,
    fail_campaign,
    get_campaign_status_snapshot,
    start_campaign_tracking,
)
from backend.app.crud import (
    bulk_store_leads,
    archive_lead,
    count_due_followups,
    count_ready_outreach,
    count_pending_leads,
    create_lead,
    get_followup_settings,
    get_lead_by_email,
    get_report_overview,
    get_sender_profile,
    leads_exist,
    list_outreach_queue,
    list_leads,
    record_open_event,
    update_sender_profile,
    update_followup_settings,
    update_lead,
)
from backend.app.database import initialize_database
from backend.app.schemas import FollowupSettingsUpdate, LeadCreate, LeadUpdate, ReplySyncRequest, SenderProfileUpdate
from backend.auth_routes import _get_user_from_token, router as auth_router
from backend.auth_models import get_user_workspaces
from backend.bulk_finder import (
    cancel_bulk_job,
    create_bulk_job,
    get_bulk_job_progress,
    get_bulk_results,
    list_bulk_jobs,
    pause_bulk_job,
    resume_bulk_job,
    start_bulk_job,
)
from backend.campaign_manager import (
    add_sequence_step,
    create_campaign,
    create_sequence,
    get_campaign,
    get_sequence_steps,
    list_campaigns,
    list_sequences,
    start_campaign_with_validation,
    update_campaign_status,
)
from backend.campaign_runner import run_campaign
from backend.config import get_deliverability_readiness, get_missing_smtp_settings, smtp_preflight_test
from backend.dashboard import (
    get_campaign_funnel,
    get_daily_stats,
    get_dashboard_stats,
    get_notifications,
    get_smtp_health,
    get_top_campaigns,
)
from backend.email_finder import EmailFinderRequest, BulkEmailFinderRequest, bulk_find_emails, find_emails, verify_email
from backend.ai_engine import generate_cold_email
from backend.env_utils import BASE_DIR
from backend.lead_crm import (
    add_lead_note,
    add_lead_tag,
    calculate_lead_score,
    delete_lead_note,
    get_lead_detail,
    get_lead_timeline,
    remove_lead_tag,
    update_lead_note,
)
from backend.reply_sync import sync_mailbox_replies
from backend.warmup_engine import (
    get_warmup_state,
    run_warmup_once,
    start_warmup,
    stop_warmup,
)
from backend.warmup_scheduler import start_warmup_scheduler


logger = logging.getLogger(__name__)

app = FastAPI(title="AI Email Automation", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/frontend", StaticFiles(directory=str(BASE_DIR / "frontend")), name="frontend")

app.include_router(auth_router)


@app.middleware("http")
async def require_api_auth(request: Request, call_next):
    path = request.url.path
    public_api = path.startswith("/api/auth/")
    if request.method != "OPTIONS" and path.startswith("/api/") and not public_api:
        try:
            request.state.user = _get_user_from_token(request.headers.get("authorization"))
        except Exception:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)
TRACKING_PIXEL_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00"
    b"\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


CSV_FIELD_ALIASES = {
    "name": "name",
    "full name": "name",
    "client name": "name",
    "email": "email",
    "e mail": "email",
    "email address": "email",
    "phone": "phone",
    "phone number": "phone",
    "mobile": "phone",
    "company": "company",
    "company name": "company",
    "industry": "industry",
    "solution": "niche",
    "service": "niche",
    "services": "niche",
    "service offering": "niche",
    "services offered": "niche",
    "offering": "niche",
    "offerings": "niche",
    "interest": "niche",
    "niche": "niche",
}


@app.on_event("startup")
def on_startup() -> None:
    initialize_database()
    from backend.auth_models import initialize_auth_tables
    from backend.campaign_manager import initialize_campaign_tables
    from backend.bulk_finder import initialize_bulk_tables
    from backend.lead_crm import initialize_crm_tables
    initialize_auth_tables()
    initialize_campaign_tables()
    initialize_bulk_tables()
    initialize_crm_tables()
    start_warmup_scheduler()


def _normalize_header(header: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (header or "").strip().lower()).strip()


def _parse_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _canonicalize_row(row: dict) -> tuple[dict, list[str]]:
    normalized = {}
    ignored = []
    for key, value in row.items():
        canonical_key = CSV_FIELD_ALIASES.get(_normalize_header(key))
        if not canonical_key:
            ignored.append(key)
            continue
        normalized[canonical_key] = (value or "").strip()
    return normalized, ignored


def _error(message: str, status_code: int, **extra):
    payload = {"error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def _request_workspace_id(request: Request) -> int:
    user = getattr(request.state, "user", None)
    workspaces = get_user_workspaces(user["id"]) if user else []
    requested = request.headers.get("x-workspace-id")
    workspace = next(
        (item for item in workspaces if requested and str(item["id"]) == requested),
        None,
    ) if requested else (workspaces[0] if workspaces else None)
    if not workspace:
        raise HTTPException(status_code=403, detail="Workspace access denied")
    return int(workspace["id"])


@app.get("/api/leads/")
def list_leads_endpoint(include_archived: bool = False):
    return list_leads(include_archived=include_archived)


@app.post("/api/leads/", status_code=201)
def create_lead_endpoint(payload: LeadCreate):
    existing = get_lead_by_email(payload.email)
    if existing:
        return _error("Lead with this email already exists.", 409)
    return create_lead(payload.model_dump())


@app.put("/api/leads/{lead_id}")
def update_lead_endpoint(lead_id: int, payload: LeadUpdate):
    try:
        lead = update_lead(lead_id, payload.model_dump())
    except ValueError as exc:
        return _error(str(exc), 409)
    if not lead:
        return _error("Lead not found or archived.", 404)
    return lead


@app.post("/api/leads/{lead_id}/archive")
def archive_lead_endpoint(lead_id: int):
    if not archive_lead(lead_id):
        return _error("Lead not found or already archived.", 404)
    return {"archived": True, "id": lead_id}


@app.post("/api/leads/upload/")
async def upload_leads(
    file: UploadFile = File(...),
    replace_existing: str = Form("true"),
    require_solution: str = Form("true"),
):
    if not file:
        return _error("No file provided", 400)

    try:
        filename = (file.filename or "").lower()
        replace_existing_value = _parse_bool(replace_existing, default=True)
        require_solution_value = _parse_bool(require_solution, default=True)
        parsed_rows = []
        ignored_columns = set()
        skipped = 0

        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            import openpyxl
            content = await file.read()
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            headers = [str(h or "").strip().lower() if h else "" for h in next(rows, [])]
            for row in rows:
                row_dict = {}
                for idx, header in enumerate(headers):
                    if header and idx < len(row):
                        row_dict[header] = row[idx]
                cleaned_row, ignored = _canonicalize_row(row_dict)
                ignored_columns.update(ignored)

                email = (cleaned_row.get("email") or "").strip().lower()
                if not email:
                    skipped += 1
                    continue

                cleaned_row["email"] = email
                if require_solution_value and not cleaned_row.get("niche"):
                    skipped += 1
                    continue
                parsed_rows.append(cleaned_row)
            wb.close()
        else:
            content = (await file.read()).decode("utf-8")
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                cleaned_row, ignored = _canonicalize_row(row)
                ignored_columns.update(ignored)

                email = (cleaned_row.get("email") or "").strip().lower()
                if not email:
                    skipped += 1
                    continue

                cleaned_row["email"] = email
                if require_solution_value and not cleaned_row.get("niche"):
                    skipped += 1
                    continue
                parsed_rows.append(cleaned_row)

        if not parsed_rows:
            return _error(
                "No valid rows found. Ensure CSV includes Email and Solution/Interest columns.",
                400,
                ignored_columns=sorted(ignored_columns),
                skipped=skipped,
            )

        created, updated = bulk_store_leads(
            parsed_rows,
            replace_existing=replace_existing_value,
        )

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "replace_existing": replace_existing_value,
            "require_solution": require_solution_value,
            "ignored_columns": sorted(ignored_columns),
        }
    except Exception as exc:
        return _error(str(exc), 400)


@app.post("/api/campaign/start/")
def start_campaign():
    if not leads_exist():
        return _error("No leads found. Upload leads before starting a campaign.", 400)

    if campaign_is_running():
        return _error(
            "Campaign is already running.",
            409,
            campaign=get_campaign_status_snapshot(),
        )

    followup_settings = get_followup_settings()
    pending_leads = count_pending_leads()
    due_followups = count_due_followups(followup_settings) if followup_settings.get("enabled") else 0
    ready_outreach = count_ready_outreach(followup_settings)
    if ready_outreach == 0:
        return _error(
            "No pending leads or due follow-ups. Upload new leads or wait until the next follow-up window.",
            400,
        )

    missing_settings = get_missing_smtp_settings()
    if missing_settings:
        missing = ", ".join(missing_settings)
        return _error(
            (
                f"Missing SMTP configuration: {missing}. "
                "Add them to the project .env file or set them in the environment before starting a campaign."
            ),
            400,
        )

    sender_profile = get_sender_profile()
    deliverability = get_deliverability_readiness(sender_profile)
    deliverability_floor = int(sender_profile.get("deliverability_floor") or 90)
    if deliverability["status"] == "blocked" or int(deliverability["score"]) < deliverability_floor:
        return _error(
            "Campaign blocked: sender deliverability is not ready for inbox-safe sending.",
            400,
            deliverability=deliverability,
        )

    # NOTE: preflight removed â€” it blocks campaign on restricted networks.
    # SMTP errors are caught per-lead and reported in campaign status.

    started, campaign = start_campaign_tracking(total=ready_outreach)
    if not started:
        return _error("Campaign is already running.", 409, campaign=campaign)

    def task():
        try:
            run_campaign(use_csv_fallback=False, only_unsent=True)
        except Exception as exc:
            fail_campaign(str(exc))
            logger.exception("Campaign thread crashed")

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


@app.get("/api/campaign/status/")
def campaign_status():
    return get_campaign_status_snapshot()


@app.get("/api/reports/overview/")
def reports_overview(days: int = 30):
    return get_report_overview(days=days)


@app.get("/api/sender/profile/")
def sender_profile():
    return get_sender_profile()


@app.post("/api/sender/profile/")
def update_sender_profile_endpoint(payload: SenderProfileUpdate):
    try:
        return update_sender_profile(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        return _error(str(exc), 400)


@app.get("/api/warmup/status/")
def warmup_status():
    return get_warmup_state()


@app.post("/api/warmup/start/")
def warmup_start():
    try:
        return start_warmup()
    except RuntimeError as exc:
        return _error(str(exc), 400)


@app.post("/api/warmup/stop/")
def warmup_stop():
    return stop_warmup()


@app.post("/api/warmup/run/")
def warmup_run():
    result = run_warmup_once()
    return result


@app.post("/api/replies/sync/")
def sync_replies(payload: ReplySyncRequest):
    try:
        return sync_mailbox_replies(limit=payload.limit, unread_only=payload.unread_only)
    except RuntimeError as exc:
        return _error(str(exc), 400)


@app.get("/t/{token}.gif", include_in_schema=False)
def track_email_open(token: str, request: Request):
    record_open_event(
        token,
        remote_addr=(request.client.host if request.client else ""),
        user_agent=request.headers.get("user-agent", ""),
    )
    return Response(
        content=TRACKING_PIXEL_BYTES,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/followups/settings/")
def get_followup_settings_endpoint():
    return get_followup_settings()


@app.get("/api/followups/settings", include_in_schema=False)
def get_followup_settings_endpoint_compat():
    return get_followup_settings()


@app.post("/api/followups/settings/")
def update_followup_settings_endpoint(payload: FollowupSettingsUpdate):
    try:
        return update_followup_settings(payload.model_dump())
    except ValueError as exc:
        return _error(str(exc), 400)


@app.post("/api/followups/settings", include_in_schema=False)
def update_followup_settings_endpoint_compat(payload: FollowupSettingsUpdate):
    try:
        return update_followup_settings(payload.model_dump())
    except ValueError as exc:
        return _error(str(exc), 400)


@app.post("/api/leads/reset-failed/")
def reset_failed_leads_endpoint():
    from backend.app.crud import reset_failed_leads
    count = reset_failed_leads()
    return {"reset": count, "message": f"{count} failed lead(s) reset to pending."}


@app.get("/api/leads/{lead_id}")
def get_lead_detail_endpoint(lead_id: int):
    lead = get_lead_detail(lead_id)
    if not lead:
        return _error("Lead not found", 404)
    return lead


@app.post("/api/leads/{lead_id}/notes")
def add_lead_note_endpoint(lead_id: int, request: Request):
    import asyncio
    body = asyncio.run(request.json())
    content = body.get("content", "")
    if not content.strip():
        return _error("Note content is required", 400)
    return add_lead_note(lead_id, content)


@app.put("/api/leads/notes/{note_id}")
def update_lead_note_endpoint(note_id: int, request: Request):
    import asyncio
    body = asyncio.run(request.json())
    content = body.get("content", "")
    result = update_lead_note(note_id, content)
    if not result:
        return _error("Note not found", 404)
    return result


@app.delete("/api/leads/notes/{note_id}")
def delete_lead_note_endpoint(note_id: int):
    delete_lead_note(note_id)
    return {"deleted": True}


@app.post("/api/leads/{lead_id}/tags")
def add_lead_tag_endpoint(lead_id: int, request: Request):
    import asyncio
    body = asyncio.run(request.json())
    tag = body.get("tag", "")
    color = body.get("color", "#FF6B00")
    if not tag.strip():
        return _error("Tag is required", 400)
    return add_lead_tag(lead_id, tag, color)


@app.delete("/api/leads/{lead_id}/tags/{tag}")
def remove_lead_tag_endpoint(lead_id: int, tag: str):
    remove_lead_tag(lead_id, tag)
    return {"deleted": True}


@app.get("/api/leads/{lead_id}/timeline")
def get_lead_timeline_endpoint(lead_id: int):
    return get_lead_timeline(lead_id)


@app.get("/api/leads/{lead_id}/score")
def get_lead_score_endpoint(lead_id: int):
    return calculate_lead_score(lead_id)


@app.get("/api/dashboard/stats/")
def dashboard_stats():
    return get_dashboard_stats()


@app.get("/api/dashboard/funnel/")
def dashboard_funnel():
    return get_campaign_funnel()


@app.get("/api/dashboard/daily/")
def dashboard_daily(days: int = 30):
    return get_daily_stats(days)


@app.get("/api/dashboard/top-campaigns/")
def dashboard_top_campaigns():
    return get_top_campaigns()


@app.get("/api/dashboard/smtp-health/")
def dashboard_smtp_health():
    return get_smtp_health()


@app.get("/api/dashboard/deliverability/")
def dashboard_deliverability():
    return get_deliverability_readiness(get_sender_profile())


@app.get("/api/dashboard/notifications/")
def dashboard_notifications(limit: int = 20):
    return get_notifications(0, limit)


@app.post("/api/email-finder/")
def email_finder_endpoint(request: EmailFinderRequest):
    return find_emails(request)


@app.post("/api/email-finder/bulk/")
def bulk_email_finder_endpoint(request: BulkEmailFinderRequest):
    return bulk_find_emails(request.prospects)


@app.post("/api/email-verifier/")
async def email_verifier_endpoint(request: Request):
    from starlette.concurrency import run_in_threadpool
    from backend.email_verifier import verify_email
    body = await request.json()
    email = body.get("email", "")
    if not email:
        return _error("Email is required", 400)
    # verify_email performs DNS + optional SMTP (blocking); run it off the
    # event loop so one slow verification cannot stall every other request.
    return await run_in_threadpool(verify_email, email)


@app.post("/api/bulk-finder/jobs/")
async def create_bulk_finder_job(request: Request):
    body = await request.json()
    name = body.get("name", "Bulk Email Find")
    prospects = body.get("prospects", [])
    if not prospects:
        return _error("No prospects provided", 400)
    return create_bulk_job(name, prospects)


@app.get("/api/bulk-finder/jobs/")
def list_bulk_finder_jobs():
    return list_bulk_jobs()


@app.get("/api/bulk-finder/jobs/{job_id}")
def get_bulk_finder_job_progress(job_id: int):
    return get_bulk_job_progress(job_id)


@app.get("/api/bulk-finder/jobs/{job_id}/results")
def get_bulk_finder_results(job_id: int, limit: int = 100, offset: int = 0):
    return get_bulk_results(job_id, limit, offset)


@app.post("/api/bulk-finder/jobs/{job_id}/start")
def start_bulk_finder_job(job_id: int):
    if start_bulk_job(job_id):
        return {"status": "started", "job_id": job_id}
    return _error("Job already running", 409)


@app.post("/api/bulk-finder/jobs/{job_id}/pause")
def pause_bulk_finder_job(job_id: int):
    pause_bulk_job(job_id)
    return {"status": "paused", "job_id": job_id}


@app.post("/api/bulk-finder/jobs/{job_id}/resume")
def resume_bulk_finder_job(job_id: int):
    resume_bulk_job(job_id)
    return {"status": "resumed", "job_id": job_id}


@app.post("/api/bulk-finder/jobs/{job_id}/cancel")
def cancel_bulk_finder_job(job_id: int):
    cancel_bulk_job(job_id)
    return {"status": "cancelled", "job_id": job_id}


@app.get("/api/campaigns/")
def list_campaigns_endpoint(request: Request):
    return list_campaigns(workspace_id=_request_workspace_id(request))


@app.post("/api/campaigns/", status_code=201)
async def create_campaign_endpoint(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return _error("Campaign payload must be a JSON object.", 400)
    workspace_id = _request_workspace_id(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _error("Campaign name is required.", 400)
    if len(name) > 200:
        return _error("Campaign name must be 200 characters or fewer.", 400)
    audience_filter = body.get("audience_filter", {})
    if not isinstance(audience_filter, dict):
        return _error("Audience filter must be a JSON object.", 400)
    allowed_filters = {"industry", "company", "niche"}
    if set(audience_filter) - allowed_filters:
        return _error("Audience filters may use industry, company, and niche.", 400)
    audience_filter = {
        key: str(value).strip()[:100]
        for key, value in audience_filter.items()
        if str(value or "").strip()
    }
    body = {
        "name": name,
        "workspace_id": workspace_id,
        "created_by": request.state.user["id"],
        "audience_filter": json.dumps(audience_filter),
        "scheduled_at": body.get("scheduled_at"),
        "sequence_id": body.get("sequence_id"),
    }
    return create_campaign(body)


@app.get("/api/campaigns/{campaign_id}")
def get_campaign_endpoint(campaign_id: int, request: Request):
    campaign = get_campaign(campaign_id, workspace_id=_request_workspace_id(request))
    if not campaign:
        return _error("Campaign not found", 404)
    return campaign


@app.post("/api/campaigns/{campaign_id}/start")
def start_saved_campaign_endpoint(campaign_id: int, request: Request):
    workspace_id = _request_workspace_id(request)
    campaign = get_campaign(campaign_id, workspace_id=workspace_id)
    if not campaign:
        return _error("Campaign not found.", 404)
    if campaign["status"] != "draft":
        return _error("Only saved drafts can be launched.", 409)
    if campaign_is_running():
        return _error("Another campaign is already running.", 409)

    try:
        filters = json.loads(campaign.get("audience_filter") or "{}")
    except (TypeError, json.JSONDecodeError):
        filters = {}
    if not isinstance(filters, dict):
        filters = {}
    queue = list_outreach_queue(get_followup_settings())
    for field in ("industry", "company", "niche"):
        expected = str(filters.get(field) or "").strip().casefold()
        if expected:
            queue = [
                lead for lead in queue
                if expected in str(lead.get(field) or "").casefold()
            ]
    if not queue:
        return _error("No eligible contacts match this campaign's audience.", 400)

    missing_settings = get_missing_smtp_settings()
    if missing_settings:
        return _error(
            f"Missing SMTP configuration: {', '.join(missing_settings)}.",
            400,
        )
    sender_profile = get_sender_profile()
    deliverability = get_deliverability_readiness(sender_profile)
    deliverability_floor = int(sender_profile.get("deliverability_floor") or 90)
    if deliverability["status"] == "blocked" or int(deliverability["score"]) < deliverability_floor:
        return _error(
            "Campaign blocked: sender deliverability is not ready for inbox-safe sending.",
            400,
            deliverability=deliverability,
        )

    lead_ids = [int(lead["id"]) for lead in queue if lead.get("id") is not None]
    started, snapshot = start_campaign_tracking(total=len(lead_ids))
    if not started:
        return _error("Another campaign is already running.", 409, campaign=snapshot)
    update_campaign_status(
        campaign_id,
        "running",
        total_leads=len(lead_ids),
        sent_count=0,
        failed_count=0,
    )

    def task():
        try:
            run_campaign(use_csv_fallback=False, only_unsent=True, lead_ids=lead_ids)
            result = get_campaign_status_snapshot()
            update_campaign_status(
                campaign_id,
                "completed",
                total_leads=result["total"],
                sent_count=result["sent"],
                failed_count=result["failed"],
            )
        except Exception as exc:
            fail_campaign(str(exc))
            result = get_campaign_status_snapshot()
            update_campaign_status(
                campaign_id,
                "failed",
                total_leads=result["total"],
                sent_count=result["sent"],
                failed_count=result["failed"],
            )
            logger.exception("Saved campaign %s crashed", campaign_id)

    threading.Thread(target=task, daemon=True).start()
    return {"status": "campaign started", "campaign": snapshot, "queue": {"total": len(lead_ids)}}


@app.get("/api/sequences/")
def list_sequences_endpoint():
    return list_sequences()


@app.post("/api/sequences/", status_code=201)
async def create_sequence_endpoint(request: Request):
    body = await request.json()
    return create_sequence(body)


@app.get("/api/sequences/{sequence_id}/steps")
def get_sequence_steps_endpoint(sequence_id: int):
    return get_sequence_steps(sequence_id)


@app.post("/api/sequences/{sequence_id}/steps", status_code=201)
async def add_sequence_step_endpoint(sequence_id: int, request: Request):
    body = await request.json()
    return add_sequence_step(sequence_id, body)


@app.post("/api/ai/generate")
async def ai_generate_endpoint(request: Request):
    body = await request.json()
    lead = body.get("lead", {}) if isinstance(body, dict) else {}
    tone = (body.get("tone") or "professional") if isinstance(body, dict) else "professional"
    goal = (body.get("goal") or "cold") if isinstance(body, dict) else "cold"
    try:
        if goal == "followup":
            step = int(body.get("step", 1))
            text = generate_cold_email(lead)
        else:
            text = generate_cold_email(lead)
        return {"email": text, "tone": tone, "goal": goal}
    except Exception as exc:
        return _error(str(exc), 500)


@app.get("/", response_class=FileResponse)
def frontend_view():
    frontend_path = BASE_DIR / "frontend" / "index.html"
    if frontend_path.exists():
        return FileResponse(
            frontend_path,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return _error("Frontend not found", 404)


@app.get("/favicon.ico")
def favicon_view():
    return Response(status_code=204)


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Run the FastAPI server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local server")
    parser.add_argument("--port", type=int, default=8000, help="Port for the local server")
    args = parser.parse_args()
    if args.serve:
        uvicorn.run(app, host=args.host, port=args.port)
