"""Email validation and bounce handling utilities."""

import re

# Common disposable email domains (expand as needed)
DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.email", "10minutemail.com",
    "guerrillamail.com", "mailinator.com", "yopmail.com",
    "trashmail.com", "fakInbox.com", "temp-mail.org",
    "sharklasers.com", "spambox.co", "grr.la"
}

# Hard bounce error keywords (don't retry)
HARD_BOUNCE_KEYWORDS = {
    "550", "551", "552", "553", "554",  # SMTP codes
    "invalid", "does not exist", "unknown user",
    "bad destination", "no such user", "undeliverable",
    "rejected", "not found", "invalid address",
    "user unknown", "invalid recipient"
}

# Soft bounce error keywords (can retry)
SOFT_BOUNCE_KEYWORDS = {
    "421", "450", "451", "452",  # SMTP codes
    "try again", "temporary", "service unavailable",
    "mailbox full", "too many connections", "throttled",
    "timeout", "temporarily"
}

# Spam filter keywords (likely spam folder, but email accepted)
SPAM_FILTER_KEYWORDS = {
    "greylisted", "rate limited", "spam", "suspicious"
}


def is_valid_email_format(email: str) -> bool:
    """Check if email has valid format."""
    if not email or not isinstance(email, str):
        return False
    
    email = email.strip().lower()
    
    # Basic regex for email format
    pattern = r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
    
    if not re.match(pattern, email):
        return False
    
    # Check for common typos
    if email.endswith((".con", ".cmo", ".ccom", ".comm")):  # .com typo
        return False
    
    return True


def is_disposable_email(email: str) -> bool:
    """Check if email uses a disposable domain."""
    if not email:
        return False
    
    try:
        domain = email.strip().lower().split("@")[1]
        return domain in DISPOSABLE_DOMAINS
    except (IndexError, AttributeError):
        return False


def classify_bounce(error_message: str) -> str:
    """
    Classify bounce type to determine retry strategy.
    Returns: 'hard', 'soft', 'spam_filter', or 'unknown'
    """
    if not error_message:
        return "unknown"
    
    error_lower = str(error_message).lower()
    
    # Check hard bounces first (highest priority - don't retry)
    for keyword in HARD_BOUNCE_KEYWORDS:
        if keyword in error_lower:
            return "hard"
    
    # Check spam filters (likely accepted but in spam)
    for keyword in SPAM_FILTER_KEYWORDS:
        if keyword in error_lower:
            return "spam_filter"
    
    # Check soft bounces (can retry)
    for keyword in SOFT_BOUNCE_KEYWORDS:
        if keyword in error_lower:
            return "soft"
    
    return "unknown"


def should_skip_email(email: str, reason: str = "") -> tuple[bool, str]:
    """
    Determine if email should be skipped (not sent).
    Returns: (should_skip: bool, reason: str)
    """
    if not is_valid_email_format(email):
        return True, "Invalid email format"
    
    if is_disposable_email(email):
        return True, "Disposable email address"
    
    # Additional checks
    email_lower = email.lower()
    
    # Skip common mailbox addresses (not real people)
    generic_addresses = {"noreply@", "no-reply@", "info@", "support@", "contact@"}
    for generic in generic_addresses:
        if email_lower.startswith(generic):
            return True, f"Generic mailbox: {generic}"
    
    return False, ""


def should_retry_email(error_message: str, current_retries: int, max_retries: int) -> bool:
    """Determine if email send should be retried based on error type."""
    bounce_type = classify_bounce(error_message)
    
    if bounce_type == "hard":
        return False  # Hard bounces - don't retry
    
    if bounce_type == "spam_filter":
        return False  # Already in spam, no point retrying
    
    if bounce_type == "soft":
        return current_retries < max_retries  # Soft bounces - retry once or twice
    
    # Unknown errors - allow retry with limit
    return current_retries < max_retries


def sanitize_email(email: str) -> str:
    """Clean and normalize email."""
    if not email:
        return ""
    return email.strip().lower()
