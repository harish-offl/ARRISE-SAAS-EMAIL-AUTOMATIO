"""Email Finder â€” discover a person's professional email from name + company/domain.

The core rule: given a person and a company (or company domain), produce
candidate professional emails and label each with an honest, evidence-derived
status. It never manufactures an email from an address string, and it never
pretends a pattern guess is a verified discovery.

Status levels (evidence-based, not fabricated):
    Verified   -> strong evidence the address is real/deliverable
    High       -> multiple supporting signals (valid MX + sensible pattern)
    Likely     -> valid domain/MX plus a plausible pattern
    Possible   -> pattern-based candidate only, no delivery evidence
    Unknown    -> insufficient evidence to evaluate

Because there is no paid/authorized data-provider integration in this
codebase, "Verified" is only returned when we have concrete external proof;
otherwise the honest status is Likely/Possible.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import dns.resolver
from pydantic import BaseModel, Field

# â”€â”€ Request models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class EmailFinderRequest(BaseModel):
    name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    domain: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)


class BulkEmailFinderRequest(BaseModel):
    prospects: list[EmailFinderRequest] = Field(max_length=1000)


# â”€â”€ Constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "live.com", "aol.com", "protonmail.com",
    "icloud.com", "me.com", "gmx.com", "yandex.com",
}

DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.email", "guerrillamail.com",
    "mailinator.com", "10minutemail.com", "yopmail.com",
}


# â”€â”€ Input validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip().lower()))


def _normalize_domain(value: str) -> str:
    """Clean a raw domain/URL into a bare host, or return '' if invalid."""
    d = value.strip().lower()
    if not d:
        return ""
    # Strip protocol and path/query/fragment
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0]
    # Strip 'www.' and trailing dot
    d = re.sub(r"^www\.", "", d)
    d = d.rstrip(".")
    # Must contain a dot and only valid domain chars
    if "." not in d or not re.match(r"^[a-z0-9.-]+$", d):
        return ""
    return d


def _resolve_company_to_domain(company: str) -> str:
    """Best-effort company name -> likely domain. Returns '' if unusable."""
    name = company.strip().lower()
    if not name:
        return ""
    if _looks_like_email(name):
        return ""
    # A clean domain like 'acme.com' passes straight through
    normalized = _normalize_domain(name)
    if normalized and "." in normalized:
        return normalized
    # Company name -> slug + .com (default TLD). Explicitly NOT an email mangling.
    slug = re.sub(r"[^a-z0-9]+", "", name)
    if not slug or len(slug) < 2:
        return ""
    return f"{slug}.com"


# â”€â”€ DNS / MX helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _check_mx(domain: str) -> dict:
    try:
        records = dns.resolver.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".") for r in records]
        return {
            "valid": True,
            "mx_records": exchanges,
            "provider": _detect_provider(exchanges),
        }
    except Exception:
        return {"valid": False, "mx_records": [], "provider": None}


def _detect_provider(mx_records: list[str]) -> str | None:
    providers = {
        "google": ["google", "gmail", "googlemail"],
        "microsoft": ["microsoft", "outlook", "hotmail", "live"],
        "zoho": ["zoho"],
        "protonmail": ["protonmail", "proton"],
        "yahoo": ["yahoo"],
        "fastmail": ["fastmail"],
        "rackmail": ["rackmail"],
    }
    for mx in mx_records:
        mx_lower = mx.lower()
        for provider, keywords in providers.items():
            if any(kw in mx_lower for kw in keywords):
                return provider
    return None


# â”€â”€ Candidate generation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _generate_candidates(first_name: str, last_name: str, domain: str) -> list[dict]:
    fn = first_name.lower().strip()
    ln = last_name.lower().strip()
    patterns = []

    def add(pattern, kind):
        if pattern and "@" in pattern and not pattern.startswith("@"):
            patterns.append({"email": pattern, "kind": kind})

    if ln:
        add(f"{fn}.{ln}@{domain}", "first.last")
        add(f"{fn}{ln}@{domain}", "firstlast")
        add(f"{fn}.{ln[0]}@{domain}", "first.lastinitial")
        add(f"{fn[0]}{ln}@{domain}", "firstinitiallast")
        add(f"{ln}.{fn}@{domain}", "last.first")
        add(f"{fn}_{ln}@{domain}", "underscore")
        add(f"{fn}-{ln}@{domain}", "hyphen")
    if fn and not ln:
        add(f"{fn}@{domain}", "firstname")
    elif fn:
        add(f"{fn}@{domain}", "firstname")
    return patterns


# â”€â”€ Main finder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _validate_input(request: EmailFinderRequest) -> dict | None:
    """Return an error payload if the input is unusable, else None."""
    domain_raw = (request.domain or "").strip()
    company = (request.company or "").strip()

    for field, value in (("domain", domain_raw), ("company", company)):
        if value and _looks_like_email(value):
            return {
                "results": [],
                "note": (
                    f"The \"{field}\" value \"{value}\" appears to be an email address. "
                    "Enter a company name or company domain instead."
                ),
            }

    if not domain_raw and not company:
        return {"results": [], "note": "Provide a company name or company domain."}

    if "@" in domain_raw:
        return {
            "results": [],
            "note": f"\"{domain_raw}\" contains '@'. This looks like an email address â€” enter a company domain instead.",
        }
    return None


def find_emails(request: EmailFinderRequest) -> dict:
    validation = _validate_input(request)
    if validation:
        return validation

    domain_raw = (request.domain or "").strip()
    company = (request.company or "").strip()

    # Resolve: use the explicit domain if given, else derive from company name.
    if domain_raw:
        domain = _normalize_domain(domain_raw)
    elif company:
        domain = _resolve_company_to_domain(company)

    if not domain:
        return {
            "results": [],
            "note": "Could not resolve a valid domain. Provide a company domain like acme.com.",
        }

    mx = _check_mx(domain)

    name = (request.name or "").strip()
    parts = name.split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""

    candidates = _generate_candidates(first_name, last_name, domain)

    results = []
    for cand in candidates:
        status, source, confidence = _classify(cand, first_name, last_name, mx)
        results.append({
            "email": cand["email"],
            "status": status,
            "confidence": confidence,
            "source": source,
            "evidence": _evidence_note(cand, mx),
            "pattern": cand["kind"],
        })

    return {
        "results": results,
        "domain": domain,
        "mx_check": mx,
        "company": company or None,
        "total_found": len(results),
        "note": _summary_note(mx, bool(first_name), bool(last_name)),
    }


def _classify(cand: dict, first_name: str, last_name: str, mx: dict) -> tuple[str, str, str]:
    """Derive (status, source, confidence) from actual signals â€” never fabricated."""
    signals = 0

    # Signal 1: a valid, non-free domain with MX records
    if mx.get("valid") and mx.get("provider") not in (None,):
        signals += 1

    # Signal 2: a full first+last name pattern (not just a first name)
    if first_name and last_name:
        signals += 1

    # Signal 3: domain is a known free provider (weak â€” likely a personal address)
    provider = mx.get("provider")
    free_like = provider is not None and any(
        kw in domain_words(cand["email"]) for kw in ("gmail", "yahoo", "outlook", "hotmail", "live")
    )

    if free_like or not mx.get("valid"):
        # Personal/free or no-mailbox domain â€” do not claim verified.
        if free_like:
            return "Possible", "pattern_generation (free-email domain)", "Low"
        return "Possible", "pattern_generation (no MX evidence)", "Low"

    if signals >= 3:
        return "High", "valid MX + full name pattern", "High"
    if signals >= 2:
        return "Likely", "valid MX + name pattern", "Medium"
    return "Possible", "pattern_generation", "Low"


def domain_words(email: str) -> str:
    return email.split("@")[-1] if "@" in email else ""


def _evidence_note(cand: dict, mx: dict) -> str:
    provider = mx.get("provider")
    if mx.get("valid") and provider:
        return f"Domain {cand['email'].split('@')[-1]} has valid MX records ({provider}). Pattern-derived address, unverified."
    if mx.get("valid"):
        return f"Domain {cand['email'].split('@')[-1]} has valid MX records. Pattern-derived address, unverified."
    if mx.get("mx_records") is not None and not mx.get("valid"):
        return "No MX records found for this domain."
    return "Pattern-based candidate only â€” no delivery evidence."


def _summary_note(mx: dict, has_first: bool, has_last: bool) -> str:
    parts = []
    if not mx.get("valid"):
        parts.append("No MX records found for the resolved domain.")
    if not has_first:
        parts.append("No person name provided.")
    elif not has_last:
        parts.append("Only a first name was provided.")
    if not parts:
        parts.append(f"Domain has a valid MX record ({mx.get('provider') or 'unknown provider'}).")
    parts.append("Results are candidates, not confirmed addresses â€” verify before sending.")
    return " ".join(parts)


# â”€â”€ Bulk â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def bulk_find_emails(requests: list[EmailFinderRequest]) -> dict:
    all_results = []
    for req in requests[:100]:
        result = find_emails(req)
        if any(e in result for e in ("note", "error")) and not result.get("results"):
            row = {"request": req.model_dump(), "results": [], "note": result.get("note", "")}
        else:
            row = {"request": req.model_dump(), "results": result["results"], "domain": result.get("domain")}
        all_results.append(row)

    return {
        "results": all_results,
        "total_prospects": len(requests),
        "processed": min(len(requests), 100),
    }


# ── Verifier (delegated to the central verification service) ──────────────────
# The verifier lives in backend.email_verifier so the single verifier, any bulk
# verifier, and the future sales-automation gate all share one engine. This
# module only re-exports it for backwards compatibility.
from backend.email_verifier import verify_email, verify_bulk  # noqa: E402,F401
