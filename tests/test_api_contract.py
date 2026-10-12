import tempfile
import unittest
from pathlib import Path
from email.message import EmailMessage

from backend.app import database
from backend.app.crud import create_lead, record_outreach_success
from backend.auth_models import initialize_auth_tables, create_user
from backend.auth import create_access_token
from backend.lead_crm import initialize_crm_tables

OWN_ADDRESS = "sender@example.com"
LEAD_EMAIL = "lead@example.com"
OUTBOUND_ID = "<outbound-message@example.com>"


def make_reply(*, body="Not interested. Please don't follow up.", message_id="<in@example.com>", provider="imap:INBOX:900"):
    msg = EmailMessage()
    msg["From"] = LEAD_EMAIL
    msg["To"] = OWN_ADDRESS
    msg["Subject"] = "Re: A note for Lead Co"
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = OUTBOUND_ID
    msg["References"] = OUTBOUND_ID
    msg.set_content(body)
    return msg.as_bytes(), provider


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database.DB_PATH = Path(self.temp_dir.name) / "api-test.db"
        database.initialize_database()
        initialize_auth_tables()
        initialize_crm_tables()
        # Create an authenticated user so the API middleware passes.
        self.user = create_user("api@test.com", "password123", "API Tester")
        self.token = create_access_token({"sub": self.user["id"]})

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_sent_lead(self):
        lead = create_lead({"name": "Lead", "email": LEAD_EMAIL, "company": "Lead Co", "niche": "SEO"})
        record_outreach_success(
            lead["id"],
            touch_type="initial",
            touch_number=0,
            subject="A note for Lead Co",
            body="Outbound body. If this is not useful, reply unsubscribe and I will not follow up.",
            message_id=OUTBOUND_ID,
        )
        return lead

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def test_lead_detail_and_timeline_after_reply_match_database_truth(self):
        from fastapi.testclient import TestClient
        from backend.reply_sync import process_inbound_message
        from backend.app.main import app

        lead = self.create_sent_lead()
        raw, provider = make_reply()
        result = process_inbound_message(raw, provider_message_id=provider, own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["reply_type"], "not_interested")

        with TestClient(app) as client:
            # Lead detail reflects the classified not_interested status
            detail = client.get(f"/api/leads/{lead['id']}", headers=self._headers()).json()
            self.assertEqual(detail["last_status"], "not_interested")
            self.assertEqual(detail["reply_type"], "not_interested")
            self.assertEqual(detail["reply_summary"], "Not interested. Please don't follow up.")

            # Timeline exposes exactly one REPLY_RECEIVED event with correct body/status
            timeline = client.get(f"/api/leads/{lead['id']}/timeline", headers=self._headers()).json()
            replies = [e for e in timeline if e["event_type"] == "REPLY_RECEIVED"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["status"], "not_interested")
            self.assertEqual(replies[0]["reply_type"], "not_interested")
            self.assertEqual(replies[0]["body"], "Not interested. Please don't follow up.")
            sent = [e for e in timeline if e["event_type"] == "EMAIL_SENT"]
            self.assertEqual(len(sent), 1)
            self.assertIn("Outbound body", sent[0]["body"])
            # No "ok", no "Not contacted" anywhere in timeline bodies
            all_bodies = " ".join(e.get("body") or "" for e in timeline).lower()
            self.assertNotIn("ok", all_bodies.split())
            self.assertNotIn("not contacted", all_bodies)

    def test_second_sync_is_duplicate_and_does_not_duplicate_api_data(self):
        from fastapi.testclient import TestClient
        from backend.reply_sync import process_inbound_message
        from backend.app.main import app

        lead = self.create_sent_lead()
        raw, provider = make_reply()
        first = process_inbound_message(raw, provider_message_id=provider, own_addresses={OWN_ADDRESS})
        self.assertEqual(first["status"], "updated")
        second = process_inbound_message(raw, provider_message_id=provider, own_addresses={OWN_ADDRESS})
        self.assertEqual(second["status"], "duplicate")

        with TestClient(app) as client:
            timeline = client.get(f"/api/leads/{lead['id']}/timeline", headers=self._headers()).json()
            replies = [e for e in timeline if e["event_type"] == "REPLY_RECEIVED"]
            self.assertEqual(len(replies), 1)

    def test_lead_edit_and_archive_are_persisted_and_remove_contact_from_queue(self):
        from fastapi.testclient import TestClient
        from backend.app.main import app

        lead = create_lead({"name": "Editable", "email": "editable@example.com", "niche": "SEO"})
        with TestClient(app) as client:
            update = client.put(
                f"/api/leads/{lead['id']}",
                headers=self._headers(),
                json={
                    "name": "Updated Contact",
                    "email": "editable@example.com",
                    "niche": "Email automation",
                    "industry": "Technology",
                    "phone": "555-0100",
                    "company": "Arrise Test",
                },
            )
            self.assertEqual(update.status_code, 200)
            self.assertEqual(update.json()["company"], "Arrise Test")

            archived = client.post(
                f"/api/leads/{lead['id']}/archive",
                headers=self._headers(),
                json={},
            )
            self.assertEqual(archived.status_code, 200)
            self.assertEqual(
                client.get("/api/leads/", headers=self._headers()).json(),
                [],
            )
            archived_rows = client.get(
                "/api/leads/?include_archived=true",
                headers=self._headers(),
            ).json()
            self.assertEqual(archived_rows[0]["archived_at"] is not None, True)
            self.assertEqual(client.get("/api/dashboard/stats/", headers=self._headers()).json()["total_leads"], 0)
            self.assertEqual(client.get("/api/dashboard/top-campaigns/", headers=self._headers()).status_code, 200)
            report = client.get("/api/reports/overview/", headers=self._headers()).json()
            self.assertEqual(report["totals"]["total_leads"], 0)

    def test_campaign_drafts_are_saved_and_workspace_scoped(self):
        from fastapi.testclient import TestClient
        from backend.app.main import app
        from backend.auth_models import create_user
        from backend.auth import create_access_token

        with TestClient(app) as client:
            created = client.post(
                "/api/campaigns/",
                headers=self._headers(),
                json={"name": "October outreach", "audience_filter": {"industry": "technology"}},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["status"], "draft")
            self.assertEqual(created.json()["name"], "October outreach")
            self.assertEqual(len(client.get("/api/campaigns/", headers=self._headers()).json()), 1)
            create_lead({
                "name": "Marketing contact",
                "email": "marketing@example.com",
                "industry": "Marketing",
            })
            no_match = client.post(
                f"/api/campaigns/{created.json()['id']}/start",
                headers=self._headers(),
                json={},
            )
            self.assertEqual(no_match.status_code, 400)
            self.assertIn("No eligible contacts", no_match.json()["error"])

            other_user = create_user("another-workspace@test.com", "password123", "Other Workspace")
            other_headers = {
                "Authorization": f"Bearer {create_access_token({'sub': other_user['id']})}",
                "Content-Type": "application/json",
            }
            self.assertEqual(client.get("/api/campaigns/", headers=other_headers).json(), [])

    def test_saved_campaign_launch_uses_only_matching_contacts(self):
        from threading import Event
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend.app import main as main_module
        from backend.app.campaign_status import finish_campaign
        from backend.app.main import app

        matching = create_lead({
            "name": "Tech contact",
            "email": "tech@example.com",
            "industry": "Technology",
            "niche": "Automation",
        })
        create_lead({
            "name": "Retail contact",
            "email": "retail@example.com",
            "industry": "Retail",
            "niche": "Automation",
        })
        finished = Event()
        status_saved = Event()
        selected_ids = []
        original_update_campaign_status = main_module.update_campaign_status

        def save_campaign_status(campaign_id, status, **values):
            original_update_campaign_status(campaign_id, status, **values)
            if status == "completed":
                status_saved.set()

        def fake_run_campaign(*, use_csv_fallback, only_unsent, lead_ids):
            selected_ids.extend(lead_ids)
            finish_campaign()
            finished.set()

        with patch.object(main_module, "get_missing_smtp_settings", return_value=[]), \
             patch.object(main_module, "get_deliverability_readiness", return_value={"status": "ready", "score": 100}), \
             patch.object(main_module, "run_campaign", side_effect=fake_run_campaign), \
        patch.object(main_module, "update_campaign_status", side_effect=save_campaign_status), \
        TestClient(app) as client:
            draft = client.post(
                "/api/campaigns/",
                headers=self._headers(),
                json={"name": "Tech launch", "audience_filter": {"industry": "tech"}},
            ).json()
            launched = client.post(
                f"/api/campaigns/{draft['id']}/start",
                headers=self._headers(),
                json={},
            )
            self.assertEqual(launched.status_code, 200)
            self.assertEqual(launched.json()["queue"]["total"], 1)
            self.assertTrue(finished.wait(2))
            self.assertTrue(status_saved.wait(2))
            saved = client.get(f"/api/campaigns/{draft['id']}", headers=self._headers()).json()
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["total_leads"], 1)

        self.assertEqual(selected_ids, [matching["id"]])


class EmailVerifierTests(unittest.TestCase):
    """Verify the honest, evidence-based verification pipeline (no fabricated results)."""

    def setUp(self):
        # Keep core-pipeline tests deterministic: no live SMTP.
        import os
        os.environ["VERIFIER_SMTP"] = "0"
        import backend.email_verifier as ev
        self.ev = ev

    def _verify(self, email, **kw):
        return self.ev.verify_email(email, **kw)

    def test_syntax_invalid_is_invalid(self):
        r = self._verify("not-an-email")
        self.assertEqual(r["status"], "INVALID")
        self.assertFalse(r["checks"]["syntax"])
        self.assertEqual(r["risk_level"], "high")

    def test_bad_domain_structure_is_invalid_domain(self):
        r = self._verify("x@example")
        self.assertEqual(r["status"], "INVALID_DOMAIN")

    def test_nonexistent_domain_is_invalid_domain(self):
        r = self._verify("x@definitely-not-a-real-domain-xyz987654321.com")
        self.assertEqual(r["status"], "INVALID_DOMAIN")
        self.assertFalse(r["deliverable"])

    def test_honest_status_never_deliverable_without_evidence(self):
        r = self._verify("someone@gmail.com", do_smtp=False)
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertFalse(r["deliverable"])
        self.assertEqual(r["mailbox_status"], "UNKNOWN")
        c = r["checks"]
        self.assertTrue(c["syntax"] and c["domain_exists"] and c["mx_configured"])
        self.assertEqual(c["mailbox_exists"], "UNKNOWN")
        self.assertGreaterEqual(len(r["evidence"]), 1)

    def test_valid_microsoft_address(self):
        r = self._verify("somebody@microsoft.com", do_smtp=False)
        c = r["checks"]
        self.assertTrue(c["syntax"] and c["domain_exists"] and c["mx_configured"])
        self.assertEqual(r["provider"], "Microsoft")

    def test_provider_detection_google(self):
        r = self._verify("x@gmail.com", do_smtp=False)
        self.assertEqual(r["provider"], "Google")

    def test_role_account_is_risky(self):
        r = self._verify("info@acmeinc.com", do_smtp=False)
        self.assertTrue(r["checks"]["role_account"])
        self.assertEqual(r["status"], "RISKY")
        self.assertEqual(r["risk_level"], "medium")
        self.assertIn("role", " ".join(r["risk_reasons"]).lower())

    def test_disposable_domain(self):
        r = self._verify("temp@mailinator.com", do_smtp=False)
        self.assertEqual(r["status"], "DISPOSABLE")
        self.assertTrue(r["checks"]["disposable"])
        self.assertEqual(r["risk_level"], "high")

    def test_no_mx_server(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_domain_probe", return_value={
                "exists": True, "status": "ok"}), \
             patch.object(self.ev, "_mx_probe", return_value={
                "mx_configured": False, "exchanges": [], "dns_status": "noanswer",
                "detail": "No MX records found."}):
            r = self._verify("x@no-mx-domain.test", do_smtp=False)
        self.assertEqual(r["status"], "NO_MAIL_SERVER")
        self.assertFalse(r["checks"]["mx_configured"])

    def test_multiple_mx_records(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_mx_probe", return_value={
                "mx_configured": True,
                "exchanges": ["mx1.example.com", "mx2.example.com"],
                "dns_status": "ok", "detail": ""}):
            r = self._verify("x@example.org", do_smtp=False)
        self.assertTrue(r["checks"]["mx_configured"])
        self.assertEqual(len(r["mx_records"]), 2)

    def test_dns_timeout_is_verification_failed_not_invalid(self):
        from unittest.mock import patch
        # Every DNS lookup (domain probe AND MX salvage) fails transiently.
        with patch.object(self.ev, "_domain_probe", return_value={
                "exists": None, "status": "timeout"}), \
             patch.object(self.ev, "_mx_probe", return_value={
                "mx_configured": None, "exchanges": [], "dns_status": "timeout",
                "detail": "DNS MX lookup timed out."}):
            r = self._verify("x@some-domain.com", do_smtp=False)
        self.assertEqual(r["status"], "VERIFICATION_FAILED")
        self.assertEqual(r["confidence"], "inconclusive")
        self.assertNotEqual(r["status"], "INVALID_DOMAIN")

    def test_dns_error_is_verification_failed_not_invalid(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_domain_probe", return_value={
                "exists": None, "status": "error"}), \
             patch.object(self.ev, "_mx_probe", return_value={
                "mx_configured": None, "exchanges": [], "dns_status": "error",
                "detail": "DNS MX lookup failed."}):
            r = self._verify("x@some-domain.com", do_smtp=False)
        self.assertEqual(r["status"], "VERIFICATION_FAILED")
        self.assertNotEqual(r["status"], "INVALID_DOMAIN")

    def test_dns_inconclusive_domain_salvaged_by_mx_records(self):
        """If the A/NS probe is flaky but MX records resolve, the domain is usable."""
        from unittest.mock import patch
        with patch.object(self.ev, "_domain_probe", return_value={
                "exists": None, "status": "timeout"}), \
             patch.object(self.ev, "_mx_probe", return_value={
                "mx_configured": True, "exchanges": ["mx.example.net"],
                "dns_status": "ok", "detail": ""}):
            r = self._verify("x@some-domain.com", do_smtp=False)
        self.assertTrue(r["checks"]["domain_exists"])
        self.assertTrue(r["checks"]["mx_configured"])
        self.assertNotEqual(r["status"], "VERIFICATION_FAILED")

    def test_smtp_accepted_is_valid_confirmed_not_deliverable(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_smtp_probe", return_value={
                "status": "ACCEPTED", "detail": "250"}):
            r = self._verify("first.last@example.org", do_smtp=True)
        self.assertEqual(r["status"], "VALID")
        self.assertEqual(r["mailbox_status"], "CONFIRMED")
        self.assertTrue(r["smtp"]["recipient_accepted"])
        self.assertFalse(r["deliverable"])  # never claim delivery from SMTP accept

    def test_smtp_rejected_is_invalid_not_confirmed(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_smtp_probe", return_value={
                "status": "REJECTED", "detail": "550 no such user"}):
            r = self._verify("nobody@example.org", do_smtp=True)
        self.assertEqual(r["status"], "INVALID")
        self.assertEqual(r["mailbox_status"], "NOT_CONFIRMED")
        self.assertEqual(r["risk_level"], "high")

    def test_smtp_timeout_is_unknown_not_invalid(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_smtp_probe", return_value={
                "status": "TIMEOUT", "detail": "timed out"}):
            r = self._verify("x@example.org", do_smtp=True)
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertEqual(r["mailbox_status"], "UNKNOWN")
        self.assertEqual(r["confidence"], "inconclusive")

    def test_smtp_temporary_failure_is_unknown(self):
        from unittest.mock import patch
        with patch.object(self.ev, "_smtp_probe", return_value={
                "status": "TEMPORARY_FAILURE", "detail": "451"}):
            r = self._verify("x@example.org", do_smtp=True)
        self.assertEqual(r["status"], "UNKNOWN")

    def test_structured_result_contract(self):
        r = self._verify("jane.smith@acmeinc.com", do_smtp=False)
        for group in ("checks", "smtp"):
            self.assertIn(group, r, f"missing {group}")
        for field in ("email", "status", "provider", "risk", "risk_reasons",
                      "confidence", "recommendation", "mailbox_status",
                      "smtp_status", "evidence", "checked_at"):
            self.assertIn(field, r, f"missing {field}")

    def test_no_fake_confidence_percentage(self):
        r = self._verify("x@gmail.com", do_smtp=False)
        self.assertNotIn("%", str(r["confidence"]))
        self.assertIn(r["confidence"], ("high", "medium", "low", "inconclusive"))

    def test_bulk_uses_same_engine(self):
        results = self.ev.verify_bulk(["bad", "x@gmail.com"], do_smtp=False)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "INVALID")
        self.assertIn(results[1]["provider"], ("Google",))

    def test_normalization_used(self):
        r = self._verify("  John.Doe@Gmail.com ", do_smtp=False)
        self.assertEqual(r["email"], "john.doe@gmail.com")
        self.assertEqual(r["normalized_email"], "john.doe@gmail.com")


class EmailVerifierHttpTests(ApiContractTests):
    """Verify the /api/email-verifier/ endpoint returns the honest contract."""

    def test_endpoint_returns_honest_result(self):
        import os
        os.environ["VERIFIER_SMTP"] = "0"  # keep it fast and deterministic
        from fastapi.testclient import TestClient
        from backend.app.main import app
        with TestClient(app) as client:
            r = client.post("/api/email-verifier/", headers=self._headers(),
                            json={"email": "someone@gmail.com"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["status"], "UNKNOWN")
            self.assertFalse(data["deliverable"])
            self.assertEqual(data["mailbox_status"], "UNKNOWN")
            self.assertIn("checks", data)
            self.assertIn("smtp", data)
            self.assertIn("recommendation", data)
            self.assertIn("checked_at", data)

    def test_endpoint_invalid_input(self):
        import os
        os.environ["VERIFIER_SMTP"] = "0"
        from fastapi.testclient import TestClient
        from backend.app.main import app
        with TestClient(app) as client:
            r = client.post("/api/email-verifier/", headers=self._headers(),
                            json={"email": ""})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
