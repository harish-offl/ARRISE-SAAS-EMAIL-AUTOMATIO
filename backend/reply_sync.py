import email
import html
import imaplib
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from backend.app.crud import (
    get_lead_by_email,
    get_lead_by_message_ids,
    mark_reply_received,
    record_reply_sync_completed,
    reply_event_already_processed,
)
from backend.config import IMAP_PORT, IMAP_SERVER, REPLY_SYNC_LOOKBACK_DAYS, get_email_credentials, get_imap_credentials


logger = logging.getLogger(__name__)
MESSAGE_ID_RE = re.compile(r"<[^>]+>")
QUOTE_MARKERS = (
    re.compile(r"^On .+ wrote:$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^_{5,}$"),
    re.compile(r"^From:\s+.+", re.IGNORECASE),
)
OUTBOUND_CUTOFF_PHRASES = (
    "if this is not useful, reply unsubscribe",
    "reply unsubscribe and i will not follow up",
    "i will not follow up",
    "this email was sent as part of",
)
SIGNATURE_MARKERS = (
    "-- ",
    "sent from my iphone",
    "sent from my android",
    "sent from outlook for",
)
AUTO_SUBMITTED_VALUES = {"auto-replied", "auto-generated"}
BULK_PRECEDENCE_VALUES = {"bulk", "list", "junk"}


class ReplySyncError(RuntimeError):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class ParsedInboundMessage:
    raw_message: bytes
    provider_message_id: str
    message_id: str
    in_reply_to: str
    references: list[str]
    subject: str
    from_email: str
    addresses: list[str]
    to_emails: list[str]
    received_at: str
    raw_body: str
    clean_body: str
    reply_type: str


class IMAPEmailProvider:
    def __init__(self, host: str, port: int, username: str, password: str, mailbox_name: str = "INBOX", timeout: int = 30):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.mailbox_name = mailbox_name
        self.timeout = timeout
        self.mailbox: imaplib.IMAP4_SSL | None = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self) -> None:
        logger.info("Connecting to IMAP mailbox %s:%s", self.host, self.port)
        self.mailbox = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        self.mailbox.login(self.username, self.password)
        status, _ = self.mailbox.select(self.mailbox_name)
        if status != "OK":
            raise ReplySyncError("IMAP_ERROR", f"Unable to select IMAP mailbox {self.mailbox_name}.")

    def search_message_uids(self, *, since: str, unread_only: bool, limit: int) -> list[bytes]:
        if self.mailbox is None:
            raise ReplySyncError("IMAP_ERROR", "IMAP mailbox is not connected.")
        criteria = ["SINCE", since]
        if unread_only:
            criteria.append("UNSEEN")
        status, payload = self.mailbox.uid("search", None, *criteria)
        if status != "OK":
            raise ReplySyncError("IMAP_ERROR", "Could not search the mailbox for replies.")
        max_items = max(1, min(int(limit or 50), 500))
        return [item for item in payload[0].split() if item][-max_items:]

    def fetch_message(self, uid: bytes) -> bytes:
        if self.mailbox is None:
            raise ReplySyncError("IMAP_ERROR", "IMAP mailbox is not connected.")
        status, fetched = self.mailbox.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            return b""
        for chunk in fetched:
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                return chunk[1]
        return b""

    def mark_seen(self, uid: bytes) -> None:
        if self.mailbox is None:
            return
        try:
            self.mailbox.uid("store", uid, "+FLAGS", "\\Seen")
        except Exception:
            logger.debug("Could not mark IMAP UID as seen", exc_info=True)

    def close(self) -> None:
        if self.mailbox is None:
            return
        try:
            self.mailbox.close()
        except Exception:
            pass
        try:
            self.mailbox.logout()
        except Exception:
            pass
        self.mailbox = None


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def _extract_message_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [match.group(0).strip() for match in MESSAGE_ID_RE.finditer(str(value))]


def _first_message_id(value: str | None) -> str:
    ids = _extract_message_ids(value)
    return ids[0] if ids else ""


def _candidate_addresses(message) -> list[str]:
    values = []
    for header in ("From", "Reply-To", "Return-Path"):
        values.extend(message.get_all(header, []))

    seen = set()
    addresses = []
    for _, address in getaddresses(values):
        normalized = address.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            addresses.append(normalized)
    return addresses


def _recipient_addresses(message) -> list[str]:
    values = []
    for header in ("To", "Delivered-To", "X-Original-To"):
        values.extend(message.get_all(header, []))
    return [address.strip().lower() for _, address in getaddresses(values) if address.strip()]


def _canonical_email(value: str | None) -> str:
    address = (value or "").strip().lower()
    local, separator, domain = address.partition("@")
    if not separator:
        return address
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _normalize_subject(value: str | None) -> str:
    subject = _decode_header_value(value)
    while subject.lower().startswith(("re:", "fwd:", "fw:")):
        subject = subject.split(":", 1)[1].strip()
    return re.sub(r"\s+", " ", subject).strip()


def _subject_matches(lead: dict, subject: str) -> bool:
    thread_subject = _normalize_subject(lead.get("thread_subject"))
    normalized_subject = _normalize_subject(subject)
    return bool(thread_subject and normalized_subject and normalized_subject.lower() == thread_subject.lower())


def _html_to_text(value: str) -> str:
    text = value or ""
    text = re.sub(r"<\s*(script|style|head)[^>]*>.*?<\s*/\s*\1\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", "\n", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<div\b[^>]*(gmail_quote|yahoo_quoted|protonmail_quote|moz-cite-prefix)[^>]*>.*?</div>", "\n", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return _collapse_body_whitespace(text)


def _collapse_body_whitespace(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned = []
    previous_blank = False
    for line in lines:
        if not line:
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    return "\n".join(cleaned).strip()


def _extract_raw_text_body(message) -> str:
    if message.is_multipart():
        preferred = []
        fallback = []
        for part in message.walk():
            content_type = str(part.get_content_type() or "").lower()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                preferred.append(text)
            elif content_type == "text/html":
                fallback.append(_html_to_text(text))
        body = "\n".join(preferred).strip() or "\n".join(fallback).strip()
    else:
        payload = message.get_payload(decode=True)
        if payload is None:
            body = ""
        else:
            charset = message.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except Exception:
                body = payload.decode("utf-8", errors="replace")
            if str(message.get_content_type() or "").lower() == "text/html":
                body = _html_to_text(body)
    return _collapse_body_whitespace(body)


def _is_quote_marker(line: str, has_content: bool) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if any(pattern.search(stripped) for pattern in QUOTE_MARKERS):
        return has_content or stripped.lower().startswith(("on ", "from:"))
    lowered = stripped.lower()
    return has_content and lowered.startswith(("sent:", "to:", "subject:"))


def _extract_clean_reply_body(raw_body: str) -> str:
    kept = []
    for line in (raw_body or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if _is_quote_marker(stripped, bool("".join(kept).strip())):
            break
        if stripped.startswith(">"):
            continue
        if lowered in SIGNATURE_MARKERS or any(lowered.startswith(marker) for marker in SIGNATURE_MARKERS):
            break
        kept.append(line)

    text = _collapse_body_whitespace("\n".join(kept))
    lowered_text = text.lower()
    cutoff_positions = [lowered_text.find(phrase) for phrase in OUTBOUND_CUTOFF_PHRASES if lowered_text.find(phrase) > 0]
    if cutoff_positions:
        text = text[: min(cutoff_positions)].strip()
    return _collapse_body_whitespace(text)[:1000]


def _message_received_at(message) -> str:
    value = message.get("Date")
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def _is_own_message(message, own_canonical_addresses: set[str]) -> bool:
    own_headers = []
    for header in ("From", "Sender"):
        own_headers.extend(message.get_all(header, []))
    return any(_canonical_email(address) in own_canonical_addresses for _, address in getaddresses(own_headers))


def _is_bounce(message, body: str) -> bool:
    from_email = parseaddr(message.get("From") or "")[1].lower()
    subject = _decode_header_value(message.get("Subject")).lower()
    content_type = str(message.get_content_type() or "").lower()
    lowered = (body or "").lower()
    return (
        "mailer-daemon" in from_email
        or "postmaster" in from_email
        or "delivery status notification" in subject
        or "delivery has failed" in lowered
        or "undeliverable" in subject
        or content_type == "message/delivery-status"
    )


def _auto_reply_type(message, body: str) -> str | None:
    lowered = (body or "").lower()
    auto_submitted = str(message.get("Auto-Submitted") or "").lower()
    precedence = str(message.get("Precedence") or "").lower()
    if auto_submitted in AUTO_SUBMITTED_VALUES:
        return "auto_reply"
    if "out of office" in lowered or "away from the office" in lowered or "automatic reply" in lowered:
        return "out_of_office"
    if precedence in BULK_PRECEDENCE_VALUES or message.get("List-Id"):
        return "auto_reply"
    return None


def _classify_reply(message, clean_body: str) -> str:
    body = (clean_body or "").lower()
    if _is_bounce(message, clean_body):
        return "bounce"
    auto_type = _auto_reply_type(message, clean_body)
    if auto_type:
        return auto_type
    if not body:
        return "unknown"
    if re.search(r"\b(unsubscribe|remove me|take me off|opt out|stop emailing)\b", body):
        return "unsubscribe"
    if re.search(r"\b(not interested|no thanks|no thank you|stop contacting|do not contact)\b", body):
        return "not_interested"
    if re.search(r"\b(pricing|price|cost|quote|proposal|package|rate card)\b", body):
        return "pricing_request"
    if re.search(r"\b(schedule|meeting|calendar|book a call|call me|let's talk|lets talk|demo)\b", body):
        return "meeting_request"
    if "?" in body or re.search(r"\b(can you|could you|what|when|where|how much|tell me more)\b", body):
        return "question"
    if re.search(r"\b(interested|send me|share details|more details|sounds good|yes|sure)\b", body):
        return "interested"
    if re.search(r"\b(great|good|thanks|thank you)\b", body) and len(body.split()) > 3:
        return "positive"
    if re.fullmatch(r"(ok|okay|k|yes|no|thanks|thank you)[.! ]*", body.strip()):
        return "unknown"
    return "unknown"


def parse_inbound_message(raw_message: bytes, *, provider_message_id: str = "") -> ParsedInboundMessage:
    message = email.message_from_bytes(raw_message)
    raw_body = _extract_raw_text_body(message)
    clean_body = _extract_clean_reply_body(raw_body)
    return ParsedInboundMessage(
        raw_message=raw_message,
        provider_message_id=provider_message_id,
        message_id=_first_message_id(message.get("Message-ID")),
        in_reply_to=_first_message_id(message.get("In-Reply-To")),
        references=_extract_message_ids(message.get("References")),
        subject=_decode_header_value(message.get("Subject")),
        from_email=parseaddr(message.get("From") or "")[1].strip().lower(),
        addresses=_candidate_addresses(message),
        to_emails=_recipient_addresses(message),
        received_at=_message_received_at(message),
        raw_body=raw_body,
        clean_body=clean_body,
        reply_type=_classify_reply(message, clean_body),
    )


def _address_matches_lead(address: str, lead: dict) -> bool:
    return _canonical_email(address) == _canonical_email(lead.get("email"))


def _find_reply_lead(parsed: ParsedInboundMessage) -> tuple[dict | None, str, str]:
    has_reply_headers = bool(parsed.in_reply_to or parsed.references)

    if parsed.in_reply_to:
        lead = get_lead_by_message_ids([parsed.in_reply_to])
        if lead:
            return lead, "in_reply_to", parsed.from_email or (parsed.addresses[0] if parsed.addresses else "")

    if parsed.references:
        lead = get_lead_by_message_ids(parsed.references)
        if lead:
            return lead, "references", parsed.from_email or (parsed.addresses[0] if parsed.addresses else "")

    if has_reply_headers:
        return None, "none", parsed.from_email or (parsed.addresses[0] if parsed.addresses else "")

    for address in parsed.addresses:
        lead = get_lead_by_email(address)
        if lead and _subject_matches(lead, parsed.subject):
            return lead, "sender_subject_fallback", address

    for address in parsed.addresses:
        canonical = _canonical_email(address)
        lead = get_lead_by_email(canonical)
        if lead and _subject_matches(lead, parsed.subject):
            return lead, "sender_subject_fallback", address

    return None, "none", parsed.from_email or (parsed.addresses[0] if parsed.addresses else "")


def process_inbound_message(raw_message: bytes, *, provider_message_id: str, own_addresses: set[str]) -> dict:
    message = email.message_from_bytes(raw_message)
    own_canonical_addresses = {_canonical_email(value) for value in own_addresses if value}
    if _is_own_message(message, own_canonical_addresses):
        return {"status": "skipped_own"}

    parsed = parse_inbound_message(raw_message, provider_message_id=provider_message_id)
    if reply_event_already_processed(provider="imap", provider_message_id=parsed.provider_message_id, message_id=parsed.message_id):
        return {"status": "duplicate", "message_id": parsed.message_id, "provider_message_id": parsed.provider_message_id}
    if not parsed.clean_body:
        return {"status": "skipped", "reason": "empty_clean_body", "message_id": parsed.message_id}
    if _auto_reply_type(message, parsed.clean_body):
        return {"status": "skipped", "reason": "auto_reply", "message_id": parsed.message_id}
    if _is_bounce(message, parsed.clean_body):
        return {"status": "skipped", "reason": "bounce", "message_id": parsed.message_id}

    lead, match_type, matched_email = _find_reply_lead(parsed)
    if not lead:
        return {"status": "skipped", "reason": "unmatched", "message_id": parsed.message_id, "subject": parsed.subject}
    if parsed.from_email and not _address_matches_lead(parsed.from_email, lead) and match_type == "sender_subject_fallback":
        return {"status": "skipped", "reason": "sender_subject_mismatch", "message_id": parsed.message_id}

    updated_lead = mark_reply_received(
        lead_id=int(lead["id"]),
        email=matched_email,
        reply_type=parsed.reply_type,
        reply_summary=parsed.clean_body,
        received_at=parsed.received_at,
        reference_message_ids=([parsed.in_reply_to] if parsed.in_reply_to else []) + parsed.references,
        provider="imap",
        provider_message_id=parsed.provider_message_id,
        inbound_message_id=parsed.message_id,
        inbound_in_reply_to=parsed.in_reply_to,
        from_email=parsed.from_email or matched_email,
        to_email=", ".join(parsed.to_emails),
        raw_body=parsed.raw_body,
        clean_body=parsed.clean_body,
        match_type=match_type,
    )
    if not updated_lead:
        return {"status": "skipped", "reason": "lead_update_failed", "message_id": parsed.message_id}
    if updated_lead.get("_duplicate_reply"):
        return {"status": "duplicate", "message_id": parsed.message_id, "provider_message_id": parsed.provider_message_id}

    return {
        "status": "updated",
        "lead_email": updated_lead.get("email") or matched_email,
        "lead_name": updated_lead.get("name") or updated_lead.get("email") or matched_email,
        "reply_type": parsed.reply_type,
        "reply_summary": parsed.clean_body,
        "subject": parsed.subject,
        "match_type": match_type,
        "message_id": parsed.message_id,
        "thread_id": None,
        "provider_message_id": parsed.provider_message_id,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _raise_sync_error(exc: Exception) -> None:
    if isinstance(exc, ReplySyncError):
        raise exc
    if isinstance(exc, imaplib.IMAP4.error):
        message = str(exc)
        lowered = message.lower()
        if "auth" in lowered or "invalid credentials" in lowered or "failure" in lowered:
            raise ReplySyncError("AUTHENTICATION_FAILED", "Gmail IMAP authentication failed. Check the mailbox address and Gmail App Password.", message) from exc
        raise ReplySyncError("IMAP_ERROR", "Gmail IMAP returned an error while syncing replies.", message) from exc
    if isinstance(exc, socket.gaierror):
        raise ReplySyncError("DNS_ERROR", "Could not resolve the Gmail IMAP hostname. Check DNS/network connectivity.", str(exc)) from exc
    if isinstance(exc, (TimeoutError, socket.timeout)):
        raise ReplySyncError("NETWORK_TIMEOUT", "Unable to reach Gmail IMAP server on TCP 993. Check outbound firewall/network connectivity.", str(exc)) from exc
    if isinstance(exc, ConnectionRefusedError):
        raise ReplySyncError("CONNECTION_REFUSED", "Gmail IMAP connection was refused on TCP 993. Check firewall/network policy.", str(exc)) from exc
    if isinstance(exc, ssl.SSLError):
        raise ReplySyncError("SSL_ERROR", "TLS negotiation with Gmail IMAP failed.", str(exc)) from exc
    if isinstance(exc, OSError):
        raise ReplySyncError("NETWORK_ERROR", "Unable to connect to Gmail IMAP. Check network/firewall connectivity.", str(exc)) from exc
    raise ReplySyncError("UNKNOWN_ERROR", "Reply sync failed unexpectedly.", str(exc)) from exc


def sync_mailbox_replies(*, limit: int = 50, unread_only: bool = False) -> dict:
    username, password = get_imap_credentials()
    if not username or not password:
        raise ReplySyncError("MISSING_CREDENTIALS", "IMAP credentials are missing. Add IMAP_USERNAME/IMAP_PASSWORD or reuse Gmail App Password.")

    checked = 0
    matched = 0
    updated = 0
    skipped = 0
    duplicates = 0
    skipped_own_messages = 0
    errors = []
    items = []
    since = (datetime.now(timezone.utc) - timedelta(days=REPLY_SYNC_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    sender_email, _ = get_email_credentials()
    own_addresses = {value.strip().lower() for value in [username, sender_email] if value}

    try:
        with IMAPEmailProvider(IMAP_SERVER, IMAP_PORT, username, password) as provider:
            uids = provider.search_message_uids(since=since, unread_only=unread_only, limit=limit)
            logger.info("Reply sync found %s candidate IMAP messages", len(uids))
            for uid in reversed(uids):
                uid_value = uid.decode("ascii", errors="replace") if isinstance(uid, bytes) else str(uid)
                provider_message_id = f"imap:{provider.mailbox_name}:{uid_value}"
                try:
                    raw_message = provider.fetch_message(uid)
                    if not raw_message:
                        skipped += 1
                        continue
                    checked += 1
                    result = process_inbound_message(raw_message, provider_message_id=provider_message_id, own_addresses=own_addresses)
                    status = result.get("status")
                    if status == "updated":
                        matched += 1
                        updated += 1
                        items.append({key: value for key, value in result.items() if key != "status"})
                        if unread_only:
                            provider.mark_seen(uid)
                    elif status == "duplicate":
                        duplicates += 1
                    elif status == "skipped_own":
                        skipped_own_messages += 1
                    else:
                        skipped += 1
                except Exception as message_exc:
                    errors.append({"provider_message_id": provider_message_id, "error": type(message_exc).__name__})
                    logger.warning("Reply sync skipped one message after processing error", exc_info=True)
    except Exception as exc:
        _raise_sync_error(exc)

    record_reply_sync_completed()
    return {
        "checked": checked,
        "matched": matched,
        "updated": updated,
        "skipped": skipped,
        "skipped_own_messages": skipped_own_messages,
        "duplicates": duplicates,
        "errors": errors[:10],
        "unread_only": bool(unread_only),
        "lookback_days": REPLY_SYNC_LOOKBACK_DAYS,
        "items": items[:10],
    }