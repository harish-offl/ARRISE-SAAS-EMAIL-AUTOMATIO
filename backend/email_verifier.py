"""Central email verification service.

This is the single verification engine shared by the single-address verifier and
any future bulk verifier. It is intentionally separate from Email Finder:

    Email Finder -> "I do not know the email. Find professional candidates."
    Email Verifier-> "I already have the email. Check whether it is usable."

Guiding rules (never violated):

* We never claim DELIVERABLE without real, reliable delivery evidence.
* We never claim a mailbox exists merely because syntax is valid, the domain
  exists, MX records exist, or an SMTP server is reachable.
* We never classify a temporary DNS/network/SMTP failure as INVALID.
* We never display hard-coded/fabricated risk or confidence percentages.

Technical failures surface as VERIFICATION_FAILED / INCONCLUSIVE / UNKNOWN,
never as a false positive or false negative about the address itself.
"""

from __future__ import annotations

import json
import re
import socket
from datetime import datetime, timezone
from typing import Optional

import dns.resolver

# ── Statuses ────────────────────────────────────────────────────────────────────
FINAL_STATUSES = {
    "VALID", "INVALID", "INVALID_DOMAIN", "NO_MAIL_SERVER", "DISPOSABLE",
    "RISKY", "DELIVERABLE", "UNKNOWN", "VERIFICATION_FAILED",
}
_MAILBOX_STATUSES = {"CONFIRMED", "NOT_CONFIRMED", "UNKNOWN"}
_SMTP_STATUSES = {
    "ACCEPTED", "REJECTED", "TEMPORARY_FAILURE", "TIMEOUT",
    "CONNECTION_ERROR", "NOT_PERFORMED", "UNKNOWN",
}

# ── Syntax / risk constants ──────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")

# Merge the disposable lists already present in the project.
from backend.email_quality import DISPOSABLE_DOMAINS as _QUALITY_DISPOSABLE  # noqa: E402

DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.email", "10minutemail.com", "guerrillamail.com",
    "mailinator.com", "yopmail.com", "trashmail.com", "temp-mail.org",
    "sharklasers.com", "spambox.co", "grr.la", "fakemail.net", "maildrop.cc",
}
DISPOSABLE_DOMAINS.update(_QUALITY_DISPOSABLE)

ROLE_LOCAL_PARTS = {
    "info", "admin", "support", "sales", "contact", "hello", "team",
    "office", "help", "service", "billing", "accounts", "careers", "hr",
    "notifications", "no-reply", "noreply", "postmaster", "webmaster",
    "abuse", "marketing", "newsletter", "general", "enquiries", "inquiries",
    "jobs", "customerservice", "sales", "media", "pr", "press",
}

_ROLE_RE = re.compile(
    r"^(info|admin|support|sales|contact|hello|team|office|help|service|billing|"
    r"accounts|careers|hr|notifications|no-?reply|postmaster|webmaster|abuse|"
    r"marketing|newsletter|general|enquir(?:ies|y)|inquir(?:ies|y)|jobs|"
    r"media|press|pr)([.+_-].*)?$"
)

# ── Provider detection ─────────────────────────────────────────────────────────
_PROVIDER_KEYWORDS = [
    ("Google", ("google", "gmail", "googlemail")),
    ("Microsoft", ("microsoft", "outlook", "hotmail", "live", "office365")),
    ("Yahoo", ("yahoo",)),
    ("Zoho", ("zoho",)),
    ("Proton Mail", ("protonmail", "proton")),
    ("Fastmail", ("fastmail",)),
    ("Apple iCloud", ("icloud", "me.com")),
    ("AOL", ("aol",)),
    ("GMX", ("gmx",)),
    ("Yandex", ("yandex",)),
    ("Mailgun", ("mailgun",)),
    ("Postmark", ("postmark",)),
    ("SendGrid", ("sendgrid",)),
    ("GoDaddy", ("secureserver", "godaddy")),
    ("Rackspace", ("rackspace", "emailsrvr")),
]


def _detect_provider(mx_records) -> Optional[str]:
    for provider, keywords in _PROVIDER_KEYWORDS:
        for mx in (mx_records or []):
            lower = str(mx).lower()
            for kw in keywords:
                if kw in lower:
                    return provider
    return None


# ── DNS helpers with explicit error classification ────────────────────────────
def _dns_timeout() -> float:
    import os
    try:
        return float(os.getenv("VERIFIER_DNS_TIMEOUT", "8"))
    except (TypeError, ValueError):
        return 8.0


def _dns_lookup(rdtype: str, name: str):
    """Returns (records, status) where status in {"ok","notfound","noanswer","timeout","error"}."""
    try:
        answers = dns.resolver.resolve(name, rdtype, lifetime=_dns_timeout())
        return list(answers), "ok"
    except dns.resolver.NXDOMAIN:
        return [], "notfound"
    except dns.resolver.NoAnswer:
        return [], "noanswer"
    except dns.resolver.NoNameservers:
        # Nameservers could not be reached -> infrastructure failure, inconclusive.
        return [], "error"
    except (dns.resolver.LifetimeTimeout, dns.exception.Timeout):
        return [], "timeout"
    except Exception:
        return [], "error"


def _domain_probe(domain: str) -> dict:
    """Determines whether the domain exists, distinguishing a real NXDOMAIN from a temp failure."""
    # Strongest signal: does the name resolve to any host/authority record?
    # Try every record type before giving up: a single transient timeout on the
    # A query must not, on its own, mask a domain that clearly has NS/MX/AAAA.
    inconclusive_status = None
    for rdtype in ("A", "AAAA", "NS", "MX"):
        records, status = _dns_lookup(rdtype, domain)
        if status == "ok":
            return {"exists": True, "status": "ok"}
        if status in ("timeout", "error"):
            inconclusive_status = inconclusive_status or status
        # "notfound"/"noanswer" -> keep probing other record types
    if inconclusive_status:
        # Some lookups failed transiently and none succeeded -> inconclusive,
        # never a hard "domain does not exist".
        return {"exists": None, "status": inconclusive_status}
    # Every record type answered NXDOMAIN/NoAnswer -> the domain does not exist.
    return {"exists": False, "status": "notfound"}


def _mx_probe(domain: str) -> dict:
    """Best-effort MX lookup with explicit classification."""
    records, status = _dns_lookup("MX", domain)
    if status == "timeout":
        return {"mx_configured": None, "exchanges": [], "dns_status": "timeout",
                "detail": "DNS MX lookup timed out."}
    if status == "error":
        return {"mx_configured": None, "exchanges": [], "dns_status": "error",
                "detail": "DNS MX lookup failed."}
    if status == "notfound" and not records:
        return {"mx_configured": False, "exchanges": [], "dns_status": "notfound",
                "detail": "Domain not found during MX lookup."}
    if not records:
        return {"mx_configured": False, "exchanges": [], "dns_status": "noanswer",
                "detail": "No MX records found."}
    exchanges = [str(r.exchange).rstrip(".") for r in records]
    return {"mx_configured": True, "exchanges": exchanges, "dns_status": "ok",
            "detail": ""}


# ── SMTP evidence (defensive, time limited, never treated as delivery proof) ──
def _smtp_probe(host: str, rcpt: str, timeout: Optional[float] = None) -> dict:
    """Connect, EHLO, MAIL FROM, RCPT TO. Returns carefully-classified outcome."""
    import os
    if timeout is None:
        try:
            timeout = float(os.getenv("VERIFIER_SMTP_TIMEOUT", "5"))
        except ValueError:
            timeout = 5.0
    if not host or not rcpt:
        return {"status": "NOT_PERFORMED", "detail": "No SMTP host available."}
    sock = None
    try:
        sock = socket.create_connection((host, 25), timeout=timeout)
        sock.settimeout(timeout)
        f = sock.makefile("rb")

        def read_code():
            # Read multiline replies (codes followed by '-').
            code = None
            data = ""
            while True:
                line = f.readline()
                if not line:
                    break
                data += line.decode("utf-8", "replace")
                if len(line) >= 4 and line[3:4] == b" ":
                    try:
                        code = int(line[:3])
                    except ValueError:
                        code = None
                    break
            return code, data

        code, _ = read_code()  # banner (220)
        if not code:
            return {"status": "CONNECTION_ERROR", "detail": "No SMTP banner."}
        sock.sendall(b"EHLO verify.local\r\n")
        code, _ = read_code()
        if code in (500, 501, 502, 503, 504, 421):
            sock.sendall(b"HELO verify.local\r\n")
            code, _ = read_code()
        sock.sendall(b"MAIL FROM:<verify@verify.local>\r\n")
        code, _ = read_code()
        if code != 250:
            return {"status": "TEMPORARY_FAILURE",
                    "detail": f"MAIL FROM rejected ({code})."}
        sock.sendall(f"RCPT TO:<{rcpt}>\r\n".encode("utf-8", "replace"))
        code, msg = read_code()
        if code == 250:
            return {"status": "ACCEPTED",
                    "detail": "Receiving server accepted the recipient (250). "
                              "Note: accept-all mail servers may report false positives."}
        if code in (450, 451, 452, 421):
            return {"status": "TEMPORARY_FAILURE",
                    "detail": f"Receiving server gave a temporary failure ({code})."}
        if code in (550, 551, 552, 553, 554):
            return {"status": "REJECTED",
                    "detail": f"Receiving server rejected the recipient ({code})."}
        return {"status": "UNKNOWN", "detail": f"Unexpected SMTP reply ({code})."}
    except socket.timeout:
        return {"status": "TIMEOUT", "detail": "SMTP connection/dialogue timed out."}
    except (ConnectionRefusedError, ConnectionResetError, OSError):
        return {"status": "CONNECTION_ERROR", "detail": "Could not reach SMTP server."}
    except Exception:
        return {"status": "UNKNOWN", "detail": "SMTP dialogue could not be completed."}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ── Helpers ────────────────────────────────────────────────────────────────────
def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _is_role_account(local_part: str) -> bool:
    local = (local_part or "").lower()
    if local in ROLE_LOCAL_PARTS:
        return True
    return bool(_ROLE_RE.match(local))


def _recommendation(status: str, role_account: bool, mailbox: str) -> str:
    if mailbox == "CONFIRMED":
        return ("Usable; the mail server accepted the recipient "
                "(delivery is still not guaranteed).")
    if mailbox == "NOT_CONFIRMED":
        return "The receiving server rejected this address; do not send."
    if status == "INVALID":
        return "Do not send: the email format is invalid."
    if status == "INVALID_DOMAIN":
        return "Do not send: the domain does not exist."
    if status == "NO_MAIL_SERVER":
        return "Do not send until the domain has valid mail routing."
    if status == "DISPOSABLE":
        return "Do not use: this is a disposable/temporary address."
    if status == "VERIFICATION_FAILED":
        return "Verification could not be completed; retry later or review manually."
    if role_account:
        return ("Technically valid, but this is a shared/role-based address and "
                "individual ownership cannot be confirmed.")
    return "Usable, but the specific mailbox existence could not be confirmed."


# ── Result builder ─────────────────────────────────────────────────────────────
def _build_result(*, email, status, syntax_valid, domain, domain_valid, mx_configured,
                  provider, disposable, role_account, mailbox_status, smtp_status,
                  smtp_detail, risk_level, risk_reasons, confidence, evidence,
                  recommendation=None, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    result = {
        "email": email,
        "normalized_email": _normalize(email),
        "status": status,
        "provider": provider,
        "risk": risk_level.upper(),
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "confidence": confidence,
        "recommendation": recommendation or _recommendation(
            status, role_account, mailbox_status or "UNKNOWN"),
        "checks": {
            "syntax": bool(syntax_valid),
            "domain_exists": domain_valid,
            "mx_configured": mx_configured,
            "mailbox_exists": mailbox_status,
            "disposable": bool(disposable),
            "role_account": bool(role_account),
        },
        "smtp": {
            "status": smtp_status or "NOT_PERFORMED",
            "detail": smtp_detail or "SMTP verification not performed.",
            "recipient_accepted": (smtp_status == "ACCEPTED"),
        },
        "mailbox_status": mailbox_status,
        "smtp_status": smtp_status,
        "evidence": evidence,
        "checked_at": checked_at,
        # Backwards-compatible flat aliases (no fabrication)
        "syntax_valid": bool(syntax_valid),
        "domain": domain,
        "domain_valid": bool(domain_valid) if domain_valid is not None else False,
        "mx_valid": bool(mx_configured) if mx_configured is not None else False,
        "disposable": bool(disposable),
        "role_account": bool(role_account),
        "deliverable": False,
        "mx_records": [],
    }
    return result


def _build_bad(*, email, status, reason, checked_at, risk_reasons=None,
               recommendation=None, syntax_valid=False, domain_valid=None,
               mx_configured=None, domain=""):
    return _build_result(
        email=email,
        status=status,
        syntax_valid=syntax_valid,
        domain=domain,
        domain_valid=domain_valid,
        mx_configured=mx_configured,
        provider=None,
        disposable=False,
        role_account=False,
        mailbox_status="UNKNOWN",
        smtp_status="NOT_PERFORMED",
        smtp_detail="",
        risk_level="high",
        risk_reasons=risk_reasons or [reason],
        confidence="high" if not syntax_valid or domain_valid is False else "inconclusive",
        evidence=[reason],
        recommendation=recommendation,
        checked_at=checked_at,
    )


# ── Storage (best-effort, self-healing) ────────────────────────────────────────
def _store_verification(result: dict) -> None:
    try:
        from backend.app import database
        with database.get_connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {database.EMAIL_VERIFICATION_TABLE} (
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
                )"""
            )
            conn.execute(
                f"""INSERT INTO {database.EMAIL_VERIFICATION_TABLE}
                    (email, normalized_email, syntax_valid, domain, domain_exists,
                     mx_configured, provider, disposable, role_account,
                     mailbox_existence, smtp_status, risk_level, risk_reasons,
                     final_status, recommendation, evidence, checked_at, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result["email"],
                    result["normalized_email"],
                    1 if result["checks"]["syntax"] else 0,
                    result["domain"] or "",
                    (1 if result["checks"]["domain_exists"] is True else 0),
                    (1 if result["checks"]["mx_configured"] is True else 0),
                    result["provider"] or "",
                    1 if result["checks"]["disposable"] else 0,
                    1 if result["checks"]["role_account"] else 0,
                    result["mailbox_status"] or "UNKNOWN",
                    result["smtp_status"] or "NOT_PERFORMED",
                    result["risk_level"],
                    json.dumps(result["risk_reasons"]),
                    result["status"],
                    result["recommendation"],
                    json.dumps(result["evidence"]),
                    result["checked_at"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        # Storage must never break verification.
        pass


# ── Public entry point ─────────────────────────────────────────────────────────
def verify_email(email: str, *, do_smtp: Optional[bool] = None) -> dict:
    """Run the full verification pipeline for a single known address."""
    import os
    if do_smtp is None:
        do_smtp = os.getenv("VERIFIER_SMTP", "1").strip().lower() not in {"0", "false", "no", "off"}
    checked_at = datetime.now(timezone.utc).isoformat()
    raw = (email or "").strip()
    normalized = _normalize(raw)

    if not normalized:
        result = _build_bad(email=raw, status="INVALID", reason="Empty input.",
                            checked_at=checked_at)
        _store_verification(result)
        return result

    # 1. SYNTAX
    syntax_valid = bool(_EMAIL_RE.match(normalized))
    if not syntax_valid or normalized.count("@") != 1:
        result = _build_bad(email=raw, status="INVALID", reason="Invalid email syntax.",
                            checked_at=checked_at)
        _store_verification(result)
        return result

    local_part, domain = normalized.split("@", 1)
    evidence = ["Valid email format."]

    # 2 + 3. DOMAIN existence / DNS
    domain_syntax_ok = bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain))
    if not domain_syntax_ok:
        result = _build_bad(email=normalized, status="INVALID_DOMAIN",
                            reason="Domain has invalid structure.",
                            risk_reasons=["Invalid domain structure"],
                            syntax_valid=True, domain_valid=False,
                            mx_configured=None, domain=domain,
                            checked_at=checked_at)
        result["evidence"] = evidence + ["Domain has invalid structure."]
        _store_verification(result)
        return result

    probe = _domain_probe(domain)
    domain_valid = probe.get("exists")  # True / False / None

    # Salvage: if domain existence was inconclusive but the MX lookup returns
    # real records, the domain provably exists and can receive mail. This turns
    # the common "A/NS query was slow, MX was fine" case into a usable result
    # instead of VERIFICATION_FAILED.
    mx = None
    if domain_valid is None:
        mx = _mx_probe(domain)
        if mx.get("mx_configured") is True:
            domain_valid = True
            evidence.append("Domain existence confirmed via MX records.")

    if domain_valid is None:
        # Temporary DNS failure -> inconclusive, never INVALID_DOMAIN.
        verdict = "VERIFICATION_FAILED"
        if probe.get("status") == "timeout":
            reason = "DNS lookup timed out; domain existence could not be confirmed."
        else:
            reason = "DNS lookup failed; domain existence could not be confirmed."
        result = _build_result(
            email=normalized, status=verdict, syntax_valid=True, domain=domain,
            domain_valid=None, mx_configured=None, provider=None,
            disposable=False, role_account=False, mailbox_status="UNKNOWN",
            smtp_status="NOT_PERFORMED", smtp_detail="",
            risk_level="unknown", risk_reasons=["DNS could not be resolved"],
            confidence="inconclusive",
            evidence=evidence + [reason], checked_at=checked_at)
        result["domain"] = domain
        result["checks"]["domain_exists"] = None
        _store_verification(result)
        return result

    if not domain_valid:
        result = _build_bad(email=normalized, status="INVALID_DOMAIN",
                            reason="Domain does not exist.",
                            risk_reasons=["Domain does not exist"],
                            syntax_valid=True, domain_valid=False,
                            mx_configured=None, domain=domain,
                            checked_at=checked_at)
        result["evidence"] = evidence + ["Domain does not exist."]
        _store_verification(result)
        return result

    if "Domain exists." not in evidence and "Domain existence confirmed via MX records." not in evidence:
        evidence.append("Domain exists.")

    # 4. MX lookup (reuse the probe already run during the salvage step)
    if mx is None:
        mx = _mx_probe(domain)
    mx_configured = mx.get("mx_configured")  # True/False/None
    if mx_configured is None:
        verdict = "VERIFICATION_FAILED"
        if mx.get("dns_status") == "timeout":
            reason = "MX lookup timed out; mail configuration could not be confirmed."
        else:
            reason = "MX lookup failed; mail configuration could not be confirmed."
        result = _build_result(
            email=normalized, status=verdict, syntax_valid=True, domain=domain,
            domain_valid=True, mx_configured=None, provider=None,
            disposable=False, role_account=False, mailbox_status="UNKNOWN",
            smtp_status="NOT_PERFORMED", smtp_detail="",
            risk_level="unknown", risk_reasons=["Mail configuration unknown"],
            confidence="inconclusive",
            evidence=evidence + [reason], checked_at=checked_at)
        _store_verification(result)
        return result

    if not mx_configured:
        result = _build_result(
            email=normalized, status="NO_MAIL_SERVER", syntax_valid=True,
            domain=domain, domain_valid=True, mx_configured=False, provider=None,
            disposable=False, role_account=False, mailbox_status="UNKNOWN",
            smtp_status="NOT_PERFORMED", smtp_detail="",
            risk_level="high", risk_reasons=["No mail (MX) server configured"],
            confidence="high",
            evidence=evidence + ["No mail server (MX) records found."],
            checked_at=checked_at)
        _store_verification(result)
        return result

    evidence.append("Mail server (MX) found.")
    provider = _detect_provider(mx["exchanges"])

    # 5. Disposable
    disposable = domain in DISPOSABLE_DOMAINS
    if disposable:
        result = _build_result(
            email=normalized, status="DISPOSABLE", syntax_valid=True,
            domain=domain, domain_valid=True, mx_configured=True, provider=provider,
            disposable=True, role_account=False, mailbox_status="UNKNOWN",
            smtp_status="NOT_PERFORMED", smtp_detail="",
            risk_level="high", risk_reasons=["Disposable/temporary email domain"],
            confidence="high",
            evidence=evidence + ["Disposable/temporary email domain detected."],
            checked_at=checked_at)
        _store_verification(result)
        return result

    # 6. Role account
    role_account = _is_role_account(local_part)

    # 7. Risk assessment (evidence-based)
    risk_level, risk_reasons = _assess_risk(domain, disposable, role_account,
                                            mx_configured, smtp=None)

    # 8. Mailbox existence (defaults to CONFIRMED only with SMTP evidence)
    mailbox_status = "UNKNOWN"

    # 9. SMTP evidence (best-effort, time-limited)
    smtp_status = "NOT_PERFORMED"
    smtp_detail = "SMTP verification not performed."
    if do_smtp and mx["exchanges"]:
        host = mx["exchanges"][0]
        smtp = _smtp_probe(host, normalized)
        smtp_status = smtp.get("status", "UNKNOWN")
        smtp_detail = smtp.get("detail", "")
        if smtp_status == "ACCEPTED":
            mailbox_status = "CONFIRMED"
            evidence.append("Receiving server accepted the recipient (SMTP).")
        elif smtp_status == "REJECTED":
            mailbox_status = "NOT_CONFIRMED"
            evidence.append("Receiving server rejected the recipient (SMTP).")
        elif smtp_status == "TEMPORARY_FAILURE":
            mailbox_status = "UNKNOWN"
            evidence.append("Receiving server gave a temporary failure; outcome unknown.")
        elif smtp_status == "TIMEOUT":
            mailbox_status = "UNKNOWN"
            evidence.append("SMTP dialogue timed out; outcome unknown.")
        elif smtp_status == "CONNECTION_ERROR":
            mailbox_status = "UNKNOWN"
            evidence.append("Could not reach the mail server; outcome unknown.")
        # re-evaluate risk with SMTP evidence
        risk_level, risk_reasons = _assess_risk(
            domain, disposable, role_account, mx_configured, smtp=smtp_status)

    # 10. Final status
    status = _final_status(syntax_valid=True, domain_valid=True,
                           mx_configured=True, disposable=disposable,
                           role_account=role_account, smtp_status=smtp_status,
                           mailbox_status=mailbox_status)

    # 11. Confidence
    confidence = _confidence(syntax_valid, domain_valid, mx_configured,
                            smtp_status, mailbox_status)

    result = _build_result(
        email=normalized, status=status, syntax_valid=True, domain=domain,
        domain_valid=True, mx_configured=True, provider=provider,
        disposable=disposable, role_account=role_account, mailbox_status=mailbox_status,
        smtp_status=smtp_status, smtp_detail=smtp_detail,
        risk_level=risk_level, risk_reasons=risk_reasons, confidence=confidence,
        evidence=evidence, checked_at=checked_at)
    result["mx_records"] = mx["exchanges"]
    _store_verification(result)
    return result


# ── Risk engine ────────────────────────────────────────────────────────────────
def _assess_risk(domain, disposable, role_account, mx_configured, smtp=None):
    """Transparent, evidence-based risk computation. Returns (level, reasons)."""
    reasons: list[str] = []
    score = 0
    if disposable:
        score += 2
        reasons.append("Disposable/temporary email domain")
    if role_account:
        score += 1
        reasons.append("Role-based address (shared mailbox)")
    if mx_configured is False:
        score += 2
        reasons.append("No mail server configured")
    if smtp == "REJECTED":
        score += 3
        reasons.append("Receiving server rejected the address")
    if smtp in ("TIMEOUT", "CONNECTION_ERROR"):
        score += 1
        reasons.append("Could not reach receiving mail server")
    elif smtp == "TEMPORARY_FAILURE":
        score += 1
        reasons.append("Receiving server reported a temporary failure")
    if mx_configured is None:
        score += 1
        reasons.append("Mail configuration could not be confirmed")
    if not reasons:
        reasons.append("No notable risk signals")

    if score >= 4 or smtp == "REJECTED":
        level = "high"
    elif score >= 2:
        level = "medium"
    elif score >= 1:
        level = "medium"
    else:
        level = "low"
    return level, reasons


def _final_status(*, syntax_valid, domain_valid, mx_configured, disposable,
                  role_account, smtp_status, mailbox_status):
    if not syntax_valid:
        return "INVALID"
    if domain_valid is False:
        return "INVALID_DOMAIN"
    if domain_valid is None or mx_configured is None:
        return "VERIFICATION_FAILED"
    if mx_configured is False:
        return "NO_MAIL_SERVER"
    if disposable:
        return "DISPOSABLE"
    if smtp_status == "REJECTED":
        return "INVALID"
    if role_account:
        return "RISKY"
    if smtp_status == "ACCEPTED":
        # The mail server accepted the recipient, but this does NOT guarantee
        # final inbox delivery, so we never claim DELIVERABLE here.
        return "VALID"
    # Otherwise the address is technically valid but the specific mailbox could
    # not be confirmed (no SMTP evidence, or SMTP was inconclusive).
    return "UNKNOWN"


def _confidence(syntax_valid, domain_valid, mx_configured, smtp_status, mailbox_status):
    """Derived from actual checks performed — never a hard-coded value.

    High confidence reflects strong evidence for whatever we DID establish.
    A confirmed negative (invalid syntax, missing domain/MX, SMTP reject) is
    high confidence in that negative. When all three core checks pass, that is
    high confidence in the verification we ran; a separate 'mailbox unknown'
    caveat is communicated via mailbox_status and evidence, not by lowering
    the confidence of the checks that actually succeeded.
    """
    if not syntax_valid or domain_valid is False or mx_configured is False:
        return "high"          # confident about the negative result
    if domain_valid is None or mx_configured is None:
        return "inconclusive"  # DNS trouble, nothing reliable was established
    if smtp_status == "REJECTED":
        return "high"          # confident the mailbox was rejected
    if smtp_status in ("TIMEOUT", "CONNECTION_ERROR", "TEMPORARY_FAILURE"):
        return "inconclusive"  # mail server did not respond -> cannot conclude
    # syntax + domain + MX all positively confirmed
    return "high"


# ── Bulk convenience (shares the same engine) ─────────────────────────────────
def verify_bulk(emails, *, do_smtp: bool = False) -> list[dict]:
    """Verify many addresses with the shared engine. SMTP is off by default."""
    return [verify_email(e, do_smtp=do_smtp) for e in (emails or [])]
