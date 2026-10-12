import html
import os
import re
import smtplib
import socket
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import quote

from backend.config import (
    SMTP_MAX_RETRIES,
    SMTP_RETRY_DELAY_SECONDS,
    get_email_credentials,
)
from backend.env_utils import load_project_env


def _get_smtp_config():
    """Read SMTP config fresh every time to avoid stale module-level values."""
    load_project_env(override=True)
    host = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    return host, port


def _env_flag(name, default=False):
    load_project_env(override=True)
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _send_plain_text_only():
    # Plain text looks closer to one-to-one mail and reduces Promotions-tab signals.
    return _env_flag("EMAIL_PLAIN_TEXT_ONLY", True)


def _clean_header(value):
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _mailto_unsubscribe(sender_email):
    safe_email = _clean_header(sender_email)
    if not safe_email:
        return ""
    subject = quote("unsubscribe", safe="")
    body = quote("Please remove me from your mailing list.", safe="")
    return f"mailto:{safe_email}?subject={subject}&body={body}"


def _email_domain(email_address):
    safe_email = _clean_header(email_address)
    if "@" not in safe_email:
        return None
    return safe_email.rsplit("@", 1)[1].strip().lower() or None


def _plain_body_with_footer(plain_body, sender_name, agency_name, sender_email=""):
    body = (plain_body or "").strip()
    if not sender_email:
        return body

    footer = "If this is not useful, reply unsubscribe and I will not follow up."
    return f"{body}\n\n{footer}"


def _apply_light_emphasis(text):
    rendered = html.escape(text or "")
    for phrase in [
        "quick 15-minute call",
        "15-minute call",
        "2 or 3 ideas",
        "2-3 ideas",
        "specific ideas",
        "reply here",
    ]:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        rendered = pattern.sub(lambda m: f"<strong>{html.escape(m.group(0))}</strong>", rendered)
    rendered = re.sub(
        r"(https?://[^\s<]+)",
        lambda m: f'<a href="{html.escape(m.group(1), quote=True)}" '
        f'style="color:#FF6B00;text-decoration:none;font-weight:600">{html.escape(m.group(1))}</a>',
        rendered,
        flags=re.IGNORECASE,
    )
    return rendered


def build_html_email(subject, plain_body, sender_name, agency_name, sender_email="", tracking_pixel_url=""):
    """Render the optional HTML version. Default sending is plain-text only."""
    sections = []
    blocks = [block.strip() for block in re.split(r"\n\s*\n", plain_body.strip()) if block.strip()]

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        if len(lines) == 2 and lines[0].lower() in {"best,", "regards,", "thanks,"}:
            sections.append(
                f'<p style="margin:0 0 4px 0;font-size:15px;line-height:1.65;color:#111827">{_apply_light_emphasis(lines[0])}</p>'
                f'<p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:#111827;font-weight:600">{_apply_light_emphasis(lines[1])}</p>'
            )
            continue

        content = "<br>".join(_apply_light_emphasis(line) for line in lines)
        sections.append(
            f'<p style="margin:0 0 16px 0;font-size:15px;line-height:1.7;color:#111827">{content}</p>'
        )

    pixel = ""
    if tracking_pixel_url:
        pixel = (
            f'<img src="{html.escape(tracking_pixel_url, quote=True)}" alt="" width="1" height="1" '
            f'style="display:block;border:0;width:1px;height:1px;opacity:0" />'
        )

    footer = ""
    if sender_email:
        safe_sender_email = html.escape(sender_email, quote=True)
        unsubscribe_mailto = html.escape(_mailto_unsubscribe(sender_email), quote=True)
        footer = (
            '<p style="margin:24px 0 0 0;font-size:11px;color:#6b7280;line-height:1.6">'
            f'If this is not useful, <a href="{unsubscribe_mailto}" style="color:#FF6B00;text-decoration:none;font-weight:500;">unsubscribe here</a> '
            f'or reply to <a href="mailto:{safe_sender_email}" style="color:#FF6B00;text-decoration:none;font-weight:500;">this email</a>.'
            "</p>"
        )

    body_html = "\n        ".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:24px;background:#F5F5F5;font-family:Arial,Helvetica,sans-serif;color:#111827">
  <div style="max-width:640px;margin:0 auto">
    {body_html}
    {footer}
    {pixel}
  </div>
</body>
</html>"""


def _connect_smtp(host, port, email_address, email_password):
    """
    Connect to SMTP.
    Port 465 uses SMTP_SSL. Port 587 uses SMTP + STARTTLS.
    Auto-falls back between ports.
    """
    # Kept for this local Windows app because verified CA chains can fail on some machines.
    ctx = ssl._create_unverified_context()

    def try_ssl(h, p):
        print(f"[SMTP] Trying SSL on {h}:{p}")
        server = smtplib.SMTP_SSL(h, p, timeout=30, context=ctx)
        server.ehlo()
        server.login(email_address, email_password)
        print(f"[SMTP] Success: Connected via SSL as {email_address}")
        return server

    def try_starttls(h, p):
        print(f"[SMTP] Trying STARTTLS on {h}:{p}")
        server = smtplib.SMTP(h, p, timeout=30)
        server.ehlo()
        server.starttls(context=ctx)
        server.ehlo()
        server.login(email_address, email_password)
        print(f"[SMTP] Success: Connected via STARTTLS as {email_address}")
        return server

    try:
        if port == 465:
            return try_ssl(host, port)
        return try_starttls(host, port)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError(
            "Gmail authentication failed. "
            "Use a Gmail App Password (not your regular password). "
            "Generate one at: myaccount.google.com -> Security -> App Passwords"
        )
    except Exception as primary_err:
        print(f"[SMTP] Primary port {port} failed: {primary_err}. Trying fallback...")

    fallback_port = 587 if port == 465 else 465
    try:
        if fallback_port == 465:
            return try_ssl(host, fallback_port)
        return try_starttls(host, fallback_port)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError(
            "Gmail authentication failed. "
            "Use a Gmail App Password (not your regular password). "
            "Generate one at: myaccount.google.com -> Security -> App Passwords"
        )
    except Exception as fallback_err:
        raise RuntimeError(
            f"Cannot connect to {host} on port {port} or {fallback_port}. "
            f"Details: {fallback_err}"
        )


class SMTPSender:
    """Reusable SMTP connection for faster multi-email sending."""

    def __init__(self):
        self.server = None
        self.email_address = None

    def connect(self):
        if self.server is not None:
            return

        if os.getenv("MOCK_SMTP", "").lower() == "true":
            print("[SMTP] MOCK mode enabled. Bypassing real SMTP connection.")
            self.email_address = "mock@example.com"

            class MockServer:
                def sendmail(self, sender, to, msg):
                    print(f"[SMTP MOCK] Sent mock email to {to}")

                def quit(self):
                    pass

            self.server = MockServer()
            return

        host, port = _get_smtp_config()
        email_address, email_password = get_email_credentials()

        if not email_address or not email_password:
            raise RuntimeError(
                "EMAIL_ADDRESS or EMAIL_PASSWORD not set. "
                "Check your .env file."
            )

        self.email_address = email_address
        self.server = _connect_smtp(host, port, email_address, email_password)

    def close(self):
        if self.server is None:
            return
        try:
            self.server.quit()
        except Exception:
            pass
        finally:
            self.server = None
            self.email_address = None

    def _apply_common_headers(self, msg, safe_to_email, safe_subject, sender_name):
        clean_sender_name = _clean_header(sender_name)
        from_header = (
            formataddr((clean_sender_name, self.email_address))
            if clean_sender_name else self.email_address
        )
        message_id = make_msgid(domain=_email_domain(self.email_address))
        msg["Subject"] = safe_subject
        msg["To"] = safe_to_email
        msg["From"] = from_header
        msg["Reply-To"] = from_header
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id

        unsubscribe_mailto = _mailto_unsubscribe(self.email_address)
        if unsubscribe_mailto:
            msg["List-Unsubscribe"] = f"<{unsubscribe_mailto}>"
        return message_id

    def _build_message(
        self,
        to_email,
        subject,
        body,
        sender_name="",
        agency_name="",
        reply_to_message_id="",
        references=None,
        tracking_pixel_url="",
    ):
        safe_subject = _clean_header(subject) or "Quick note"
        safe_to_email = _clean_header(to_email)
        plain_body = _plain_body_with_footer(body, sender_name, agency_name, self.email_address)

        if _send_plain_text_only():
            msg = MIMEText(plain_body, "plain", "utf-8")
            message_id = self._apply_common_headers(msg, safe_to_email, safe_subject, sender_name)
        else:
            msg = MIMEMultipart("alternative")
            message_id = self._apply_common_headers(msg, safe_to_email, safe_subject, sender_name)
            html_body = build_html_email(
                safe_subject,
                body,
                sender_name,
                agency_name,
                sender_email=self.email_address,
                tracking_pixel_url=tracking_pixel_url,
            )
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        if reply_to_message_id:
            msg["In-Reply-To"] = _clean_header(reply_to_message_id)
        if references:
            if isinstance(references, (list, tuple)):
                references_value = " ".join(_clean_header(ref) for ref in references if ref)
            else:
                references_value = _clean_header(references)
            if references_value:
                msg["References"] = references_value

        return msg, message_id, safe_to_email

    def send(
        self,
        to_email,
        subject,
        body,
        sender_name="",
        agency_name="",
        reply_to_message_id="",
        references=None,
        tracking_pixel_url="",
    ):
        attempts = SMTP_MAX_RETRIES + 1
        last_error = None
        safe_to_email = _clean_header(to_email)

        for attempt in range(attempts):
            try:
                self.connect()
                msg, message_id, safe_to_email = self._build_message(
                    safe_to_email,
                    subject,
                    body,
                    sender_name,
                    agency_name,
                    reply_to_message_id=reply_to_message_id,
                    references=references,
                    tracking_pixel_url=tracking_pixel_url,
                )
                self.server.sendmail(self.email_address, safe_to_email, msg.as_string())
                return message_id
            except Exception as exc:
                last_error = exc
                self.close()
                if attempt < attempts - 1:
                    time.sleep(SMTP_RETRY_DELAY_SECONDS)

        raise RuntimeError(f"Failed to send to {safe_to_email}: {last_error}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def send_email(to_email, subject, body, sender_name="", agency_name=""):
    """Backward-compatible single send API."""
    with SMTPSender() as sender:
        return sender.send(to_email, subject, body, sender_name, agency_name)