from dataclasses import dataclass


@dataclass(slots=True)
class Lead:
    id: int
    email: str
    name: str = ""
    niche: str = ""
    industry: str = ""
    phone: str = ""
    company: str = ""
    sent_at: str | None = None
    last_status: str = "pending"
    last_error: str = ""
    last_contacted_at: str | None = None
    next_followup_at: str | None = None
    followup_count: int = 0
    sequence_status: str = "pending"
    reply_received_at: str | None = None
    opt_out_at: str | None = None
    sequence_completed_at: str | None = None
    thread_subject: str = ""
    last_message_id: str = ""
    first_open_at: str | None = None
    last_open_at: str | None = None
    open_count: int = 0
    reply_type: str = ""
    reply_summary: str = ""
