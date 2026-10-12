# Arrise Digital — Email Automation

A local email automation application for B2B outreach. Import contacts, generate personalized emails, send campaigns through configured SMTP, and review recorded campaign activity. The interface uses Arrise Digital brand colors and defaults to a light theme, with an optional dark theme.

**Local backend:** Python + FastAPI + Uvicorn

---

## Current features

- Public Arrise Digital marketing site with platform overview, workflow, and pricing/access sections
- Responsive authentication pages and authenticated dashboard (HTML + CSS, no framework)
- CSV / Excel lead upload with header normalization
- Industry-specific randomised bullet points — no two leads receive the same set
- Approved B2B cold email template with P.S. line
- Gmail SMTP sending via port 465 SSL with auto-fallback to 587
- Real-time campaign monitor — leads move from Pending → Sent live
- Leads Workspace with search, status filters, contact editing, and safe archiving
- Saved campaign drafts with optional industry audience matching and explicit launch checks
- Dashboard metrics and activity ranges backed by persisted reports
- Python FastAPI backend with SQLite storage and Ollama AI generation

## Deployment status

This repository currently provides a local application, not a production-ready multi-tenant SaaS. Saved campaign drafts are scoped to the signed-in user's workspace, but contact, sender, reporting, and live sending operations are not yet consistently scoped to workspace membership. Campaign work runs in a process-local background thread; scheduled campaigns and delayed automation are not durable across server restarts. Do not expose this instance to untrusted users or use it to send campaigns until tenant isolation, persistent job processing, and production deployment safeguards are implemented.

The public site is served at `/` before sign-in. Pricing is presented as early access because subscriptions, payment processing, and commercial plans have not been configured.

---

## Project Structure

```
/
├── frontend/                   # Local Python server frontend
│   ├── index.html
│   └── styles.css
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI routes
│   │   ├── crud.py             # DB operations
│   │   ├── models.py           # SQLAlchemy models
│   │   ├── schemas.py          # Pydantic schemas
│   │   ├── database.py         # SQLite init
│   │   └── campaign_status.py  # In-memory campaign state
│   ├── ai_engine.py            # Email generation + bullet library
│   ├── campaign_runner.py      # Parallel SMTP workers
│   ├── smtp_sender.py          # SMTP connection + HTML builder
│   ├── config.py               # Env config + validation
│   ├── env_utils.py            # .env loader
│   └── main.py                 # Entry point (--serve / --migrate)
├── data/
│   └── leads.csv               # Sample leads
└── .env                        # Local environment variables (gitignored)
```

---

## Quick Start (Local)

**1. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure `.env`**

```env
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=465
EMAIL_ADDRESS=your@gmail.com
EMAIL_PASSWORD=your_app_password
DATABASE_URL=leads.db
SENDER_NAME=Your Name
AGENCY_NAME=Your Company Name
MAX_CONCURRENT_EMAILS=2
DELAY_BETWEEN_EMAILS=0
SMTP_MAX_RETRIES=1
```

> Gmail requires an **App Password** — generate one at:
> myaccount.google.com → Security → 2-Step Verification → App Passwords

**3. Run the server**

```bash
python -m backend.app.main --serve
```

Open **http://localhost:8000**

---

## CSV Format

Upload a CSV with these columns (case-insensitive, aliases supported):

| Column | Aliases |
|--------|---------|
| Name | Full Name, Client Name |
| Email | Email Address |
| Company | Company Name |
| Industry | — |
| Solution | Niche, Interest, Service, Offering |
| Phone | Phone Number, Mobile |

**Example:**

```csv
Name,Email,Company,Industry,Solution
Annamalai,annamalai@example.com,TechNova Solutions,IT Services,App Development
Ram Viswanth,ram@example.com,Nair Fashions,Retail,Social Media Marketing
```

---

## Email Template

Every email follows this exact structure:

```
Subject: {Solution} growth strategy for {Company}

Hi {FirstName},

As a professional in the {Industry} sector, you know how important
it is to stay ahead of competitors and maintain steady growth.

With competition increasing and buyer behavior shifting online, many
{Industry} businesses find it difficult to generate consistent
qualified leads and maintain visibility.

At {AgencyName}, we help {Industry} businesses improve online growth
through {Solution} tailored to their market and customer intent.

- [Industry-specific bullet 1]
- [Industry-specific bullet 2]
- [Industry-specific bullet 3]

Would you be open to a quick 15-minute call to explore how {Solution}
can help {Company} attract more clients?

Best regards,
{SenderName}

P.S. Many businesses in {Industry} are already using {Solution} to
capture more demand. This is a strong time to stay ahead.
```

---

## Industry Bullet Library

The system has a curated bullet library for 13 industries. Each industry has 7 bullets — 3 are picked randomly per lead so no two leads receive the same combination.

**Supported industries:** IT Services, Real Estate, Healthcare, Education, Finance, Retail, Manufacturing, Hospitality, Logistics, Legal, App Development, Digital Marketing, Ecommerce.

To add a new industry, edit `_BULLET_LIBRARY` in `backend/ai_engine.py`.

---

## API Endpoints (Local Python Backend)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/leads/` | List all leads |
| POST | `/api/leads/` | Create a single lead |
| POST | `/api/leads/upload/` | Upload CSV file |
| POST | `/api/campaign/start/` | Start background campaign |
| GET | `/api/campaign/status/` | Get campaign status |

**Upload options (form fields):**
- `replace_existing` (default: `true`) — delete old leads before import
- `require_solution` (default: `true`) — skip rows missing niche/solution

---

## SMTP Notes

- Port **465** (SSL) is used by default — more reliable on restricted networks
- Auto-fallback to port **587** (STARTTLS) if 465 fails
- Uses `ssl._create_unverified_context()` to bypass Windows CA cert chain issues
- Gmail App Password required — regular password will not work

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Connection timed out (10060)` | Network blocking port 465/587 — try mobile hotspot |
| `Authentication failed` | Use Gmail App Password, not regular password |
| `No valid rows found` | CSV must have `Email` and `Solution`/`Niche` column |
| `Campaign failed immediately` | Check `.env` has `EMAIL_ADDRESS` and `EMAIL_PASSWORD` |
| Leads not updating live | Hard refresh browser (`Ctrl+Shift+R`) |
| All leads get same bullets | Check `Industry` column is populated in CSV |

---

## Security

- Never commit `.env` with real credentials — it is gitignored
- Use Gmail App Passwords only
- `ai_generation.log` is gitignored
- SQLite `.db` files are gitignored
---

## Inbox Delivery Setup

No app can guarantee inbox placement, but campaigns should not be started until the sender is ready:

- Use a real mailbox with a good reputation; do not send from a fresh account at high volume.
- Publish SPF, DKIM, and DMARC for your sending domain. If using Gmail/Google Workspace, enable DKIM in the Google Admin console and add the DNS record Google gives you.
- Keep `OPEN_TRACKING_ENABLED=false` unless `TRACKING_BASE_URL` is a public HTTPS URL on the same sender domain or subdomain.
- Keep `DAILY_SEND_LIMIT` at 25 or lower for new/cold mailboxes, then increase slowly only after replies are healthy and bounces are low.
- Include a clear unsubscribe path. The sender now adds both visible unsubscribe text and a `List-Unsubscribe` header automatically.

Check readiness from the running app:

```text
http://127.0.0.1:8001/api/dashboard/deliverability/
```

Start locally on port 8001:

```powershell
python -m backend.app.main --serve --host 127.0.0.1 --port 8001
```
