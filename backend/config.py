import os
import re

from backend.env_utils import load_project_env

# Always load with override=True so .env values win over stale system env vars
load_project_env(override=True)

# ── SMTP config ---------------------------------------------------------------
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))

# ── Campaign tuning -----------------------------------------------------------
DELAY_BETWEEN_EMAILS   = float(os.getenv("DELAY_BETWEEN_EMAILS", "0"))
MAX_CONCURRENT_EMAILS  = max(1, int(os.getenv("MAX_CONCURRENT_EMAILS", "2")))
SMTP_MAX_RETRIES       = max(0, int(os.getenv("SMTP_MAX_RETRIES", "1")))
SMTP_RETRY_DELAY_SECONDS = float(os.getenv("SMTP_RETRY_DELAY_SECONDS", "1"))
LEAD_FETCH_CHUNK_SIZE  = max(100, int(os.getenv("LEAD_FETCH_CHUNK_SIZE", "500")))
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
REPLY_SYNC_LOOKBACK_DAYS = max(1, int(os.getenv("REPLY_SYNC_LOOKBACK_DAYS", "21")))


def _clean_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_email_credentials() -> tuple[str | None, str | None]:
    """Always re-read from env so runtime changes are picked up."""
    load_project_env(override=True)
    return _clean_env("EMAIL_ADDRESS"), _clean_env("EMAIL_PASSWORD")


def get_imap_credentials() -> tuple[str | None, str | None]:
    """Use explicit IMAP creds when provided, otherwise reuse SMTP mailbox creds."""
    load_project_env(override=True)
    username = _clean_env("IMAP_USERNAME")
    password = _clean_env("IMAP_PASSWORD")
    if username and password:
        return username, password
    return get_email_credentials()


def get_sender_identity_defaults() -> dict[str, object]:
    load_project_env(override=True)
    email_address, _ = get_email_credentials()
    sender_name = _clean_env("SENDER_NAME")
    agency_name = _clean_env("AGENCY_NAME")
    website_url = _clean_env("WEBSITE_URL") or ""
    tracking_base_url = _clean_env("TRACKING_BASE_URL") or ""
    return {
        "sender_name": sender_name or ((email_address or "your").split("@")[0] or "Your Name"),
        "agency_name": agency_name or "Your Company Name",
        "website_url": website_url,
        "tracking_base_url": tracking_base_url.rstrip("/"),
    }


def get_missing_smtp_settings() -> list[str]:
    email_address, email_password = get_email_credentials()
    missing = []
    if not email_address:
        missing.append("EMAIL_ADDRESS")
    if not email_password:
        missing.append("EMAIL_PASSWORD")
    return missing


def validate_smtp_config() -> None:
    """
    Validate SMTP config at startup.
    Raises RuntimeError with a clear message if anything is wrong.
    """
    load_project_env(override=True)

    server = os.getenv("SMTP_SERVER", "").strip()
    port_str = os.getenv("SMTP_PORT", "587").strip()
    email, password = get_email_credentials()

    errors = []

    # Must not be an IP address — Gmail requires hostname for TLS SNI
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", server):
        errors.append(
            f"SMTP_SERVER is set to an IP address ({server}). "
            "Use the hostname 'smtp.gmail.com' instead — "
            "Gmail requires the hostname for TLS SNI negotiation."
        )
    elif not server:
        errors.append("SMTP_SERVER is not set. Add SMTP_SERVER=smtp.gmail.com to your .env file.")

    try:
        port = int(port_str)
        if port not in (587, 465, 25):
            errors.append(f"SMTP_PORT={port} is unusual. Expected 587 (STARTTLS) or 465 (SSL).")
    except ValueError:
        errors.append(f"SMTP_PORT='{port_str}' is not a valid integer.")

    if not email:
        errors.append("EMAIL_ADDRESS is not set. Add it to your .env file.")
    if not password:
        errors.append("EMAIL_PASSWORD is not set. Add your Gmail App Password to .env.")

    if errors:
        raise RuntimeError(
            "SMTP configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def smtp_preflight_test() -> str:
    """
    Test SMTP connection before campaign starts.
    Returns 'ok' on success, raises RuntimeError with clear message on failure.
    """
    import smtplib
    import socket

    validate_smtp_config()

    server = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
    port   = int(os.getenv("SMTP_PORT", "587"))
    email, password = get_email_credentials()

    print(f"[SMTP preflight] host={server} port={port} user={email}")

    try:
        if port == 465:
            import ssl
            context = ssl._create_unverified_context()
            with smtplib.SMTP_SSL(server, port, timeout=15, context=context) as conn:
                conn.ehlo()
                conn.login(email, password)
        else:
            with smtplib.SMTP(server, port, timeout=15) as conn:
                conn.ehlo()
                if port == 587:
                    conn.starttls()
                    conn.ehlo()
                conn.login(email, password)
        print("[SMTP preflight] Connection successful")
        return "ok"

    except smtplib.SMTPAuthenticationError:
        raise RuntimeError(
            "Gmail authentication failed. "
            "Make sure you are using a Gmail App Password (not your regular password). "
            "Generate one at: myaccount.google.com -> Security -> App Passwords"
        )
    except smtplib.SMTPConnectError as e:
        raise RuntimeError(
            f"Cannot connect to {server}:{port}. "
            "Check your internet connection and firewall. "
            f"Detail: {e}"
        )
    except TimeoutError:
        raise RuntimeError(
            f"Connection to {server}:{port} timed out (error 10060). "
            "Your network or firewall is blocking outbound port 587. "
            "Try: disable VPN, check Windows Firewall, or use a mobile hotspot to test."
        )
    except socket.gaierror:
        raise RuntimeError(
            f"Cannot resolve hostname '{server}'. "
            "Check your internet connection or DNS settings."
        )
    except Exception as e:
        raise RuntimeError(f"SMTP preflight failed: {e}")


def _extract_domain(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    if "@" in text and "://" not in text:
        return text.rsplit("@", 1)[1].strip(" .")
    from urllib.parse import urlparse

    parsed = urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or "").strip(" .")


def _read_txt_records(name: str) -> list[str]:
    try:
        import dns.resolver

        records = dns.resolver.resolve(name, "TXT", lifetime=5)
        values = []
        for record in records:
            chunks = getattr(record, "strings", None)
            if chunks:
                values.append("".join(part.decode("utf-8", "ignore") for part in chunks))
            else:
                values.append(str(record).strip('"'))
        return values
    except Exception:
        return []


def _has_mx_record(domain: str) -> bool:
    try:
        import dns.resolver

        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:
        return False


def get_deliverability_readiness(sender_profile: dict | None = None) -> dict:
    """Return inbox-readiness checks used to block obviously risky sends.

    This cannot guarantee inbox placement; mailbox providers make the final
    decision. It does prevent sends when the sender/domain setup is likely to
    damage deliverability.
    """
    load_project_env(override=True)
    email_address, _ = get_email_credentials()
    sender_profile = sender_profile or {}
    email_domain = _extract_domain(email_address)
    tracking_domain = _extract_domain(sender_profile.get("tracking_base_url"))
    website_domain = _extract_domain(sender_profile.get("website_url"))
    dkim_selector = _clean_env("DKIM_SELECTOR")

    checks: list[dict[str, object]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def add_check(name: str, ok: bool, detail: str, severity: str = "warning") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity})
        if ok:
            return
        if severity == "blocker":
            blockers.append(detail)
        else:
            warnings.append(detail)

    add_check(
        "sender_email",
        bool(email_address and email_domain),
        "EMAIL_ADDRESS must be set to a real mailbox before sending.",
        "blocker",
    )

    if email_domain:
        add_check(
            "mx_record",
            _has_mx_record(email_domain),
            f"{email_domain} must have MX records so replies and reputation checks work.",
            "blocker",
        )

        txt_records = _read_txt_records(email_domain)
        has_spf = any(record.lower().startswith("v=spf1") for record in txt_records)
        add_check(
            "spf_record",
            has_spf,
            f"{email_domain} should publish an SPF TXT record authorizing the mail provider.",
            "blocker",
        )

        dmarc_records = _read_txt_records(f"_dmarc.{email_domain}")
        has_dmarc = any("v=dmarc1" in record.lower() for record in dmarc_records)
        add_check(
            "dmarc_record",
            has_dmarc,
            f"{email_domain} should publish a DMARC record to improve inbox trust.",
            "blocker",
        )

        if dkim_selector:
            dkim_records = _read_txt_records(f"{dkim_selector}._domainkey.{email_domain}")
            has_dkim = any("v=dkim1" in record.lower() or "p=" in record.lower() for record in dkim_records)
            add_check(
                "dkim_record",
                has_dkim,
                f"DKIM selector {dkim_selector} was not found for {email_domain}.",
                "blocker",
            )
        else:
            add_check(
                "dkim_record",
                True,
                "DKIM could not be verified because DKIM_SELECTOR is not configured; add it for stricter checks.",
                "info",
            )

    if sender_profile.get("open_tracking_enabled"):
        add_check(
            "tracking_url",
            bool(tracking_domain) and tracking_domain not in {"localhost", "127.0.0.1", "0.0.0.0"},
            "Open tracking requires a public HTTPS tracking domain, not localhost.",
            "blocker",
        )
        tracking_url = str(sender_profile.get("tracking_base_url") or "").strip().lower()
        add_check(
            "tracking_https",
            tracking_url.startswith("https://"),
            "Open tracking should use HTTPS to avoid security and spam-filter penalties.",
            "blocker",
        )
        if tracking_domain and email_domain:
            aligned = tracking_domain == email_domain or tracking_domain.endswith(f".{email_domain}")
            add_check(
                "tracking_alignment",
                aligned,
                "Tracking domain should be aligned with the sender domain or disabled for colder mailboxes.",
                "warning",
            )

    if website_domain and email_domain:
        aligned = website_domain == email_domain or website_domain.endswith(f".{email_domain}")
        add_check(
            "website_alignment",
            aligned,
            "Sender website domain should match or sit under the sender email domain.",
            "warning",
        )

    daily_limit = int(sender_profile.get("daily_send_limit") or os.getenv("DAILY_SEND_LIMIT", "25") or 25)
    add_check(
        "daily_limit",
        daily_limit <= 50,
        "Daily send limit above 50 is aggressive for cold outreach; ramp slowly to protect inbox placement.",
        "warning",
    )

    if not sender_profile.get("warmup_enabled"):
        add_check(
            "warmup",
            True,
            "Warmup mode is off; keep volume conservative until reply and bounce rates are stable.",
            "info",
        )

    score = max(0, 100 - (len(blockers) * 25) - (len(warnings) * 4))
    status = "blocked" if blockers else ("at_risk" if score < 90 else "ready")
    return {
        "status": status,
        "score": score,
        "sender_domain": email_domain,
        "tracking_domain": tracking_domain,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "note": "Inbox placement cannot be guaranteed, but these checks reduce obvious spam-folder risk.",
    }
