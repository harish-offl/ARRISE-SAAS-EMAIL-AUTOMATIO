from typing import Optional

from pydantic import BaseModel, Field


class LeadCreate(BaseModel):
    name: Optional[str] = None
    email: str
    niche: Optional[str] = None
    industry: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None


class LeadUpdate(BaseModel):
    name: Optional[str] = None
    email: str
    niche: Optional[str] = None
    industry: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None


class LeadRead(LeadCreate):
    id: int
    sent_at: Optional[str] = None
    last_status: str = "pending"
    last_error: str = ""
    last_contacted_at: Optional[str] = None
    next_followup_at: Optional[str] = None
    followup_count: int = 0
    sequence_status: str = "pending"
    reply_received_at: Optional[str] = None
    opt_out_at: Optional[str] = None
    sequence_completed_at: Optional[str] = None
    thread_subject: str = ""
    last_message_id: str = ""
    first_open_at: Optional[str] = None
    last_open_at: Optional[str] = None
    open_count: int = 0
    reply_type: str = ""
    reply_summary: str = ""
    archived_at: Optional[str] = None


class FollowupSettingsUpdate(BaseModel):
    enabled: bool = False
    followup_1_delay_days: int = Field(default=3, ge=0, le=90)
    followup_2_delay_days: int = Field(default=7, ge=0, le=90)
    final_bump_delay_days: int = Field(default=12, ge=0, le=90)
    followup_1_date: Optional[str] = None
    followup_2_date: Optional[str] = None
    final_bump_date: Optional[str] = None


class FollowupSettingsRead(FollowupSettingsUpdate):
    due_count: int = 0
    scheduled_count: int = 0
    active_count: int = 0
    next_due_at: Optional[str] = None


class SenderProfileUpdate(BaseModel):
    sender_name: Optional[str] = None
    agency_name: Optional[str] = None
    website_url: Optional[str] = None
    tracking_base_url: Optional[str] = None
    open_tracking_enabled: Optional[bool] = None
    reply_sync_enabled: Optional[bool] = None
    warmup_enabled: Optional[bool] = None
    warmup_status: Optional[str] = None
    warmup_recipients: Optional[str] = None
    daily_send_limit: Optional[int] = Field(default=None, ge=1, le=500)
    daily_warmup_target: Optional[int] = Field(default=None, ge=1, le=500)
    deliverability_floor: Optional[int] = Field(default=None, ge=50, le=100)
    spam_guard_enabled: Optional[bool] = None
    snov_workspace_url: Optional[str] = None


class ReplySyncRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    unread_only: bool = False
