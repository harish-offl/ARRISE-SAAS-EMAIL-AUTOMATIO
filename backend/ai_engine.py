import os
import re
import random
import subprocess

from backend.env_utils import load_project_env

try:
    import requests  # type: ignore
except Exception:
    requests = None

load_project_env()

# name of the ollama model to use (must be installed locally or accessible via ollama)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_REQUEST_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_REQUEST_TIMEOUT_SECONDS", "180"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "100"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.4"))
MIN_BODY_WORDS = int(os.getenv("MIN_COLD_EMAIL_WORDS", "95"))
MAX_BODY_WORDS = int(os.getenv("MAX_COLD_EMAIL_WORDS", "150"))
DEFAULT_SENDER_NAME = os.getenv("SENDER_NAME", os.getenv("EMAIL_ADDRESS", "Your Name").split("@")[0] or "Your Name")
DEFAULT_COMPANY_NAME = os.getenv("AGENCY_NAME", "Your Company Name")
DEFAULT_WEBSITE_URL = os.getenv("WEBSITE_URL", "").strip()


# ── Industry-specific bullet point library ───────────────────────────────────
# Each industry has 6+ bullets. 3 are picked randomly per lead so no two
# leads in the same industry receive the same combination.
_BULLET_LIBRARY: dict[str, list[str]] = {
    "it services": [
        "Faster project delivery through streamlined digital workflows",
        "Higher client retention with automated follow-up and onboarding",
        "Increased inbound leads from decision-makers searching for IT solutions",
        "Stronger positioning against offshore competitors in local markets",
        "Reduced sales cycle length with targeted outreach to qualified prospects",
        "Improved brand credibility through consistent digital presence",
        "More demo bookings from LinkedIn and search-driven campaigns",
    ],
    "real estate": [
        "More qualified property inquiries from serious buyers and investors",
        "Faster listing visibility through targeted digital campaigns",
        "Higher conversion rate from lead to site visit",
        "Stronger local brand recognition in competitive property markets",
        "Automated follow-up sequences that keep prospects engaged",
        "Increased referral traffic from content and social proof strategies",
        "Better ROI on marketing spend with data-driven targeting",
    ],
    "healthcare": [
        "More patient appointments booked through digital channels",
        "Stronger trust and credibility with online reputation management",
        "Increased visibility for specialist services in local search",
        "Higher patient retention through automated communication workflows",
        "Reduced no-show rates with timely reminder and engagement campaigns",
        "Improved referral rates from satisfied patients and partner clinics",
        "Faster growth in new patient acquisition without increasing ad spend",
    ],
    "education": [
        "Higher student enrollment through targeted digital outreach",
        "Improved course visibility across search and social platforms",
        "Stronger parent and student engagement with automated communication",
        "Increased brand authority as a trusted institution in your region",
        "Better lead-to-enrollment conversion with nurture campaigns",
        "More referrals from current students and alumni networks",
        "Reduced cost per enrollment through optimised digital funnels",
    ],
    "finance": [
        "More qualified leads from high-intent financial service seekers",
        "Stronger compliance-safe digital presence across key platforms",
        "Increased trust and credibility with professional content strategies",
        "Higher conversion from prospect to client with targeted follow-up",
        "Improved visibility for niche financial products in competitive markets",
        "Better client retention through value-driven email communication",
        "Faster pipeline growth without relying solely on referrals",
    ],
    "retail": [
        "Higher foot traffic and online store visits through targeted campaigns",
        "Improved repeat purchase rate with loyalty-driven email sequences",
        "Stronger seasonal campaign performance with data-backed targeting",
        "Increased average order value through personalised product outreach",
        "Better brand recall in a crowded retail market",
        "More customer reviews and social proof driving new buyer confidence",
        "Reduced cart abandonment with automated recovery campaigns",
    ],
    "manufacturing": [
        "More B2B leads from procurement managers and supply chain decision-makers",
        "Stronger industry positioning through thought leadership content",
        "Increased RFQ volume from targeted outreach to qualified buyers",
        "Better visibility at trade shows and industry directories online",
        "Improved distributor and partner acquisition through digital channels",
        "Faster quote-to-order conversion with streamlined follow-up",
        "Reduced dependency on cold calling with inbound lead generation",
    ],
    "hospitality": [
        "Higher direct bookings reducing dependency on third-party platforms",
        "Stronger guest loyalty through personalised post-stay campaigns",
        "Increased visibility during peak travel and event seasons",
        "Better online reputation management driving more 5-star reviews",
        "More corporate and group booking inquiries through targeted outreach",
        "Improved occupancy rates with data-driven promotional campaigns",
        "Stronger brand presence across travel search and social platforms",
    ],
    "logistics": [
        "More inbound inquiries from businesses seeking reliable logistics partners",
        "Stronger positioning against national carriers in regional markets",
        "Increased contract renewals through proactive client communication",
        "Better visibility for specialised freight and last-mile services",
        "Improved lead quality from targeted B2B outreach campaigns",
        "Faster client onboarding with automated proposal and follow-up flows",
        "Higher referral rate from satisfied clients through structured programs",
    ],
    "legal": [
        "More qualified client inquiries from people actively seeking legal help",
        "Stronger local search visibility for your practice areas",
        "Improved client trust through consistent thought leadership content",
        "Higher consultation booking rate from targeted digital campaigns",
        "Better referral pipeline from professional networks and past clients",
        "Increased brand authority in competitive legal service categories",
        "Faster case pipeline growth without relying solely on word of mouth",
    ],
    "app development": [
        "More project inquiries from startups and enterprises seeking development partners",
        "Stronger portfolio visibility across tech communities and search",
        "Higher client retention through proactive project communication",
        "Increased inbound leads from decision-makers evaluating app vendors",
        "Better positioning against offshore development firms in local markets",
        "Faster sales cycle with targeted outreach to qualified tech buyers",
        "Improved brand credibility through case studies and client success stories",
    ],
    "digital marketing": [
        "More agency clients from businesses actively searching for marketing help",
        "Stronger case study visibility driving inbound interest",
        "Higher retainer conversion from one-time project clients",
        "Improved positioning as a results-driven agency in your niche",
        "Faster new client acquisition through targeted cold outreach",
        "Better referral rate from satisfied clients through structured programs",
        "Increased brand authority through consistent thought leadership content",
    ],
    "ecommerce": [
        "Higher conversion rate from product page visitors to buyers",
        "Improved customer lifetime value through retention email campaigns",
        "Stronger brand visibility across search and social shopping platforms",
        "Increased repeat purchase rate with personalised product recommendations",
        "Better abandoned cart recovery with automated follow-up sequences",
        "More product reviews and social proof driving new buyer confidence",
        "Faster revenue growth through data-driven promotional targeting",
    ],
}

# Generic fallback bullets used when industry is not in the library
_GENERIC_BULLETS: list[str] = [
    "Increased qualified website traffic and online visibility",
    "Improved lead generation from digital channels",
    "Stronger brand authority in the local market",
    "Higher conversion rate from prospect to paying client",
    "Better client retention through consistent digital communication",
    "Faster business growth with targeted outreach campaigns",
    "Improved ROI on marketing spend through data-driven strategies",
    "More referrals and word-of-mouth through reputation management",
]


def _get_bullets(industry: str, solution: str) -> list[str]:
    """
    Return 3 unique bullets for this lead.
    Matches industry to the library (case-insensitive, partial match).
    Randomises selection so no two leads get the same combination.
    """
    industry_lower = (industry or "").lower().strip()
    solution_lower = (solution or "").lower().strip()

    # Try exact or partial match against library keys
    pool = None
    for key, bullets in _BULLET_LIBRARY.items():
        if key in industry_lower or industry_lower in key:
            pool = bullets
            break

    # Also try matching against solution/niche
    if pool is None:
        for key, bullets in _BULLET_LIBRARY.items():
            if key in solution_lower or solution_lower in key:
                pool = bullets
                break

    if pool is None:
        pool = _GENERIC_BULLETS

    # Pick 3 unique bullets, randomised
    count = min(3, len(pool))
    return random.sample(pool, count)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences that may appear in CLI output."""
    return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", text or "")


def _ascii_safe(text: str) -> str:
    """Normalize generated text so SMTP headers/body stay portable."""
    return (text or "").encode("ascii", "ignore").decode("ascii").strip()


def _lead_value(lead: dict, key: str, default: str) -> str:
    value = str(lead.get(key, "") or "").strip()
    return value if value else default


def _build_prompt(lead: dict) -> str:
    name        = _lead_value(lead, "name",     "there")
    company     = _lead_value(lead, "company",  "your business")
    solution    = _lead_value(lead, "niche",    "digital growth")
    industry    = _lead_value(lead, "industry", "your industry")
    sender_name = DEFAULT_SENDER_NAME
    agency_name = DEFAULT_COMPANY_NAME

    return (
        f"Write a cold email using EXACTLY this structure. "
        f"Replace the placeholders with the values provided. Output ONLY the final email.\n\n"
        f"VALUES:\n"
        f"FirstName = {name}\n"
        f"Industry = {industry}\n"
        f"AgencyName = {agency_name}\n"
        f"Service = {solution}\n"
        f"CompanyName = {company}\n"
        f"SenderName = {sender_name}\n\n"
        f"TEMPLATE (copy exactly, only replace values):\n\n"
        f"Subject: {solution} growth strategy for {company}\n\n"
        f"Hi {name},\n\n"
        f"As a professional in the {industry} sector, you know how important it is to stay ahead of competitors and maintain steady growth.\n\n"
        f"With competition increasing and buyer behavior shifting online, many {industry} businesses find it difficult to generate consistent qualified leads and maintain visibility.\n\n"
        f"At {agency_name}, we help {industry} businesses improve online growth through {solution} tailored to their market and customer intent.\n\n"
        f"- Increased qualified website traffic and online visibility\n"
        f"- Improved lead generation from digital channels\n"
        f"- Stronger brand authority in the local market\n\n"
        f"Would you be open to a quick 15-minute call to explore how {solution} can help {company} attract more clients?\n\n"
        f"Best regards,\n"
        f"{sender_name}\n\n"
        f"P.S. Many businesses in {industry} are already using {solution} to capture more demand. This is a strong time to stay ahead.\n\n"
        f"STRICT RULES:\n"
        f"1. Start with Subject: line\n"
        f"2. Only ONE greeting: Hi {name}, — never add Dear\n"
        f"3. Each bullet on its OWN line starting with '- '\n"
        f"4. Signature = two lines: 'Best regards,' then '{sender_name}' on next line\n"
        f"5. P.S. line is mandatory\n"
        f"6. One blank line between every paragraph\n"
        f"7. Plain text only — no HTML, no markdown, no emoji\n"
        f"8. Do NOT change the structure — only personalise the content\n"
        f"9. Keep body under 150 words"
    )




def _generate_with_ollama(prompt: str) -> tuple[str, str]:
    """Try Ollama HTTP API first, then CLI fallbacks for compatibility."""
    errors = []

    if requests is not None:
        try:
            response = requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    "options": {
                        "num_predict": OLLAMA_NUM_PREDICT,
                        "temperature": OLLAMA_TEMPERATURE,
                    },
                },
                timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            generated_text = _strip_ansi(payload.get("response", "")).strip()
            if generated_text:
                return generated_text, ""
            errors.append("ollama http: empty response")
        except Exception as exc:
            errors.append(f"ollama http: {exc}")

    commands = [
        ["ollama", "run", OLLAMA_MODEL, prompt],
        ["ollama", "generate", OLLAMA_MODEL, "--prompt", prompt],
    ]

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            return _strip_ansi(proc.stdout).strip(), ""
        except FileNotFoundError as exc:
            errors.append(f"{' '.join(cmd[:3])}: {exc}")  # type: ignore
            break
        except subprocess.CalledProcessError as exc:
            stderr = _strip_ansi(exc.stderr).strip()
            errors.append(f"{' '.join(cmd[:3])}: {stderr or str(exc)}")  # type: ignore

    return "", " | ".join(errors)


def _format_email(text: str, sender_name: str) -> str:
    """
    Post-processor: cleans, restructures and normalises every email.
    Rules applied:
    - Remove standalone brand/company line at top
    - Keep only one greeting (Hi ...,) — drop Dear
    - Vertical bullet points, never inline
    - Blank line between every paragraph
    - Two-line signature: Best regards,\\n{name}
    - Remove incomplete footer/unsubscribe text
    - P.S. on its own paragraph
    """
    if not text:
        return text

    lines = text.splitlines()

    # ── 1. Separate subject from body ──────────────────────────────────────
    subject_line = ""
    body_lines = lines
    if lines and lines[0].strip().lower().startswith("subject:"):
        subject_line = lines[0].strip()
        body_lines = lines[1:]

    # ── 2. Remove leading blank lines ──────────────────────────────────────
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]

    # ── 3. Remove standalone brand/company line at top ─────────────────────
    agency = DEFAULT_COMPANY_NAME.strip().lower()
    if body_lines and body_lines[0].strip().lower() == agency:
        body_lines = body_lines[1:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]

    # ── 4. Remove duplicate greeting — keep only "Hi ..., " ────────────────
    greeting_seen = False
    cleaned = []
    for line in body_lines:
        s = line.strip().lower()
        is_hi   = s.startswith("hi ") and s.endswith(",")
        is_dear = s.startswith("dear ") and s.endswith(",")
        if is_hi or is_dear:
            if not greeting_seen:
                if is_dear:
                    name_part = line.strip()[5:].rstrip(",").strip()
                    cleaned.append(f"Hi {name_part},")
                else:
                    cleaned.append(line.strip())
                greeting_seen = True
            # skip any subsequent greeting line
        else:
            cleaned.append(line)
    body_lines = cleaned

    # ── 5. Fix inline bullets ───────────────────────────────────────────────
    expanded = []
    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith("- ") and re.search(r"\s-\s", stripped[2:]):
            parts = re.split(r"\s+-\s+", stripped)
            for i, part in enumerate(parts):
                part = part.strip().lstrip("- ").strip()
                if part:
                    expanded.append(f"- {part}")
        else:
            expanded.append(line)
    body_lines = expanded

    # ── 6. Rejoin and fix signature on one line ────────────────────────────
    rejoined = "\n".join(body_lines)
    rejoined = re.sub(
        r"(Best regards),\s+([A-Za-z][^\n]+)",
        r"\1,\n\2",
        rejoined,
        flags=re.IGNORECASE,
    )

    # ── 7. Remove incomplete footer / unsubscribe text ─────────────────────
    rejoined = re.sub(
        r"\n+You received this email because.*$", "",
        rejoined, flags=re.IGNORECASE | re.DOTALL
    )
    rejoined = re.sub(
        r"\n+To unsubscribe.*$", "",
        rejoined, flags=re.IGNORECASE | re.DOTALL
    )

    # ── 8. Ensure blank line before bullets ────────────────────────────────
    rejoined = re.sub(r"([^\n])\n(- )", r"\1\n\n\2", rejoined)

    # ── 9. Ensure blank line after bullet block ────────────────────────────
    rejoined = re.sub(r"(^- [^\n]+)(\n)([^-\n])", r"\1\n\n\3", rejoined, flags=re.MULTILINE)

    # ── 10. Ensure blank line before "Best regards" ────────────────────────
    rejoined = re.sub(r"([^\n])\n(Best regards)", r"\1\n\n\2", rejoined, flags=re.IGNORECASE)

    # ── 11. Ensure blank line before P.S. ─────────────────────────────────
    rejoined = re.sub(r"([^\n])\n(P\.S\.)", r"\1\n\n\2", rejoined, flags=re.IGNORECASE)

    # ── 12. Collapse 3+ blank lines → 2 ───────────────────────────────────
    rejoined = re.sub(r"\n{3,}", "\n\n", rejoined)

    # ── 13. Reassemble with subject ───────────────────────────────────────
    final = f"{subject_line}\n\n{rejoined.strip()}" if subject_line else rejoined.strip()
    return final.strip()



def _normalize_email(raw_text: str, lead: dict) -> str:
    name     = _lead_value(lead, "name",     "there")
    solution = _lead_value(lead, "niche",    "digital growth")
    industry = _lead_value(lead, "industry", "your industry")

    lines = [line.rstrip() for line in (raw_text or "").splitlines()]
    lines = [line for line in lines if line.strip()]

    if not lines:
        return _detailed_fallback(lead)

    first = _ascii_safe(lines[0].strip())
    if first.lower().startswith("subject:"):
        subject   = first
        body_lines = lines[1:]
    else:
        subject    = f"Subject: {solution} growth strategy for {_lead_value(lead, 'company', 'your business')}"
        body_lines = lines

    if not body_lines:
        return _detailed_fallback(lead)

    body = _ascii_safe("\n".join(body_lines).strip())

    # Word count gate
    body_word_count = _word_count(body)
    if body_word_count < MIN_BODY_WORDS or body_word_count > MAX_BODY_WORDS:
        return _detailed_fallback(lead)

    final_text = _ascii_safe(f"{subject}\n{body}")

    # Must contain solution phrase
    if not _solution_alignment_ok(final_text, solution):
        return _detailed_fallback(lead)

    # Must pass structure check
    if not _structure_alignment_ok(final_text, name, industry):
        return _detailed_fallback(lead)

    # CRITICAL: reject if bullets are inline (all on one line separated by spaces)
    # A valid email must have at least 3 lines that each start with "- "
    bullet_lines = [l for l in body_lines if l.strip().startswith("- ")]
    if len(bullet_lines) < 3:
        return _detailed_fallback(lead)

    # Reject if "Dear" appears (old format)
    if any(l.strip().lower().startswith("dear ") for l in body_lines):
        return _detailed_fallback(lead)

    # Reject if "Here's what you can expect" appears (old format)
    if any("here's what you can expect" in l.lower() for l in body_lines):
        return _detailed_fallback(lead)

    return _format_email(final_text, DEFAULT_SENDER_NAME)



def _detailed_fallback(lead: dict) -> str:
    """Guaranteed-format fallback with industry-specific randomised bullets."""
    name        = _lead_value(lead, "name",     "there")
    company     = _lead_value(lead, "company",  "your business")
    solution    = _lead_value(lead, "niche",    "digital growth")
    industry    = _lead_value(lead, "industry", "your industry")
    sender_name = DEFAULT_SENDER_NAME
    agency_name = DEFAULT_COMPANY_NAME

    # Get 3 unique industry-specific bullets — randomised per lead
    bullets = _get_bullets(industry, solution)
    bullet_lines = "\n".join(f"- {b}" for b in bullets)

    email = (
        f"Subject: {solution} growth strategy for {company}\n"
        f"\n"
        f"Hi {name},\n"
        f"\n"
        f"As a professional in the {industry} sector, you know how important it is to stay ahead of competitors and maintain steady growth.\n"
        f"\n"
        f"With competition increasing and buyer behavior shifting online, many {industry} businesses find it difficult to generate consistent qualified leads and maintain visibility.\n"
        f"\n"
        f"At {agency_name}, we help {industry} businesses improve online growth through {solution} tailored to their market and customer intent.\n"
        f"\n"
        f"{bullet_lines}\n"
        f"\n"
        f"Would you be open to a quick 15-minute call to explore how {solution} can help {company} attract more clients?\n"
        f"\n"
        f"Best regards,\n"
        f"{sender_name}\n"
        f"\n"
        f"P.S. Many businesses in {industry} are already using {solution} to capture more demand. This is a strong time to stay ahead."
    )

    return _ascii_safe(email)





def _solution_alignment_ok(email_text: str, solution: str) -> bool:
    expected = (solution or "").strip().lower()
    if not expected:
        return True

    lines = email_text.splitlines()
    if not lines:
        return False

    subject = lines[0].lower()
    body = "\n".join(lines[1:]).lower()  # type: ignore
    return expected in subject and expected in body


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _structure_alignment_ok(email_text: str, name: str, industry: str) -> bool:
    lines = [line.strip().lower() for line in email_text.splitlines() if line.strip()]
    if len(lines) < 8:
        return False
    lowered = "\n".join(lines)
    if f"hi {name.lower()}," not in lowered:
        return False
    if "dear" in lowered:
        return False  # reject if Dear slipped in
    if "best regards" not in lowered:
        return False
    if "15-minute call" not in lowered:
        return False
    if "p.s." not in lowered:
        return False
    bullet_lines = [l for l in lines if l.startswith("- ")]
    return len(bullet_lines) >= 3




def _log_result(result: str, error: str) -> None:
    try:
        with open("ai_generation.log", "a", encoding="utf-8") as logf:
            if error:
                logf.write(f"[OLLAMA_ERROR] {error}\n")
            logf.write(result + "\n---\n")
    except Exception:
        pass


def _profile_value(sender_profile: dict | None, key: str, fallback: str) -> str:
    if sender_profile and sender_profile.get(key):
        return str(sender_profile.get(key) or "").strip() or fallback
    return fallback


def _website_line(sender_profile: dict | None = None) -> str:
    website = _profile_value(sender_profile, "website_url", DEFAULT_WEBSITE_URL)
    if not website:
        return ""
    if "://" not in website:
        website = f"https://{website}"
    return website.rstrip("/")


def _soften_outcome_phrase(value: str) -> str:
    text = (value or "").lower()
    replacements = {
        "targeted campaigns": "clearer follow-up",
        "digital campaigns": "digital presence",
        "campaigns": "follow-up",
        "campaign": "follow-up",
        "promotional": "practical",
        "automated recovery": "simple reminder",
        "automated follow-up sequences": "simple follow-up notes",
        "email sequences": "email follow-up",
        "lead generation": "inquiry handling",
        "inbound leads": "inbound inquiries",
        "qualified leads": "qualified inquiries",
        "sales cycle": "decision process",
        "conversion rate": "response rate",
        "roi on marketing spend": "clarity on what is working",
        "revenue growth": "steady growth",
        "buyer confidence": "customer confidence",
        "brand authority": "trust",
        "brand visibility": "visibility",
        "brand recall": "recognition",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.strip()

def _build_human_subject(company: str, solution: str) -> str:
    options = [
        f"Quick question about {company}",
        f"A note for {company}",
        f"Thought this may be useful for {company}",
        f"Question about {solution}",
    ]
    return _ascii_safe(random.choice(options))


def _build_human_body(lead: dict, sender_profile: dict | None = None) -> str:
    name = _lead_value(lead, "name", "there")
    company = _lead_value(lead, "company", "your team")
    solution = _lead_value(lead, "niche", "growth work")
    industry = _lead_value(lead, "industry", "your space")
    sender_name = _profile_value(sender_profile, "sender_name", DEFAULT_SENDER_NAME)
    website = _website_line(sender_profile)

    opener = random.choice(
        [
            f"I came across {company} and had a small thought about {solution}.",
            f"I was looking at {company} and wanted to send a short note about {solution}.",
            f"Reaching out because {solution} looked relevant to the work {company} is doing.",
        ]
    )
    context = random.choice(
        [
            f"I noticed a few places where the next step could probably be made clearer for someone new looking at {company}.",
            f"In {industry}, small clarity changes often make it easier for the right person to understand what to do next.",
            f"My thought was simple: make the first message and follow-up path easier for someone to respond to.",
        ]
    )
    close = random.choice(
        [
            f"Would it be useful if I sent 2 or 3 notes specific to {company}?",
            f"Happy to send a short, specific suggestion for {company} if it would help.",
            f"If this is relevant, I can send a quick written idea and you can decide from there.",
        ]
    )

    lines = [
        f"Hi {name},",
        "",
        opener,
        "",
        context,
        "",
        close,
        "",
        "Best,",
        sender_name,
    ]
    if website:
        lines.extend(["", website])
    return _ascii_safe("\n".join(lines))


def assess_email_quality(subject: str, body: str) -> dict:
    text = f"{subject}\n{body}"
    lowered = text.lower()
    flags = []

    if len(re.findall(r"https?://", body, flags=re.IGNORECASE)) > 1:
        flags.append("too_many_links")
    if text.count("!") > 1:
        flags.append("heavy_exclamation")
    if re.search(r"\b[A-Z]{4,}\b", text):
        flags.append("all_caps_word")
    if re.search(r"\b(free|guaranteed|risk-free|act now|urgent|limited time|winner)\b", lowered):
        flags.append("spam_trigger_phrase")
    promotional_hits = re.findall(
        r"\b(campaign|campaigns|promotion|promotional|discount|deal|offer|roi|revenue|cart abandonment|sales pipeline)\b",
        lowered,
    )
    if len(promotional_hits) >= 2:
        flags.append("promotions_tab_language")
    if len(subject.split()) > 8:
        flags.append("long_subject")
    if len(body.splitlines()) > 10:
        flags.append("too_many_blocks")

    score = 100 - (18 * len(flags))
    if "http://" in lowered:
        flags.append("non_https_link")
        score -= 8

    return {
        "score": max(0, min(100, score)),
        "flags": flags,
    }


def generate_cold_email(lead: dict, sender_profile: dict | None = None) -> str:
    company = _lead_value(lead, "company", "your team")
    solution = _lead_value(lead, "niche", "growth work")
    subject = _build_human_subject(company, solution)
    body = _build_human_body(lead, sender_profile)
    result = _ascii_safe(f"Subject: {subject}\n\n{body}")
    _log_result(result, "")
    return result


def _reply_subject(subject: str, lead: dict) -> str:
    base_subject = _ascii_safe((subject or "").strip())
    if not base_subject:
        base_subject = _ascii_safe(
            f"{_lead_value(lead, 'niche', 'digital growth')} growth strategy for "
            f"{_lead_value(lead, 'company', 'your business')}"
        )
    while base_subject.lower().startswith("re:"):
        base_subject = base_subject[3:].strip()
    return f"Re: {base_subject}"


def generate_followup_email(
    lead: dict,
    step_number: int,
    thread_subject: str = "",
    sender_profile: dict | None = None,
) -> str:
    name = _lead_value(lead, "name", "there")
    company = _lead_value(lead, "company", "your business")
    solution = _lead_value(lead, "niche", "digital growth")
    industry = _lead_value(lead, "industry", "your industry")
    sender_name = _profile_value(sender_profile, "sender_name", DEFAULT_SENDER_NAME)
    website = _website_line(sender_profile)
    subject = _reply_subject(thread_subject, lead)
    bullets = _get_bullets(industry, solution)

    if step_number == 1:
        body = (
            f"Subject: {subject}\n"
            f"\n"
            f"Hi {name},\n"
            f"\n"
            f"Wanted to quickly circle back on my earlier note about {solution} for {company}.\n"
            f"\n"
            f"A lot of teams in {industry} are mainly trying to improve {bullets[0].lower()} and {bullets[1].lower()} without adding more manual work.\n"
            f"\n"
            f"If helpful, I can send over a couple of specific ideas for {company} or keep it to a quick 15-minute call.\n"
            f"\n"
            f"Best,\n"
            f"{sender_name}"
        )
    elif step_number == 2:
        body = (
            f"Subject: {subject}\n"
            f"\n"
            f"Hi {name},\n"
            f"\n"
            f"Following up once more in case {solution} is still on the radar for {company}.\n"
            f"\n"
            f"We usually help {industry} teams improve {bullets[0].lower()} and {bullets[1].lower()} without adding extra sales overhead.\n"
            f"\n"
            f"Would it make sense to share 2 or 3 practical ideas for {company}?\n"
            f"\n"
            f"Best,\n"
            f"{sender_name}"
        )
    else:
        body = (
            f"Subject: {subject}\n"
            f"\n"
            f"Hi {name},\n"
            f"\n"
            f"I will close the loop after this note.\n"
            f"\n"
            f"If improving results through {solution} is a priority for {company}, I am happy to send a concise plan or set up a quick 15-minute call.\n"
            f"\n"
            f"If not, no worries and I will leave it here.\n"
            f"\n"
            f"Best,\n"
            f"{sender_name}"
        )

    if website:
        body = f"{body}\n\n{website}"

    result = _ascii_safe(body)
    _log_result(result, "")
    return result
