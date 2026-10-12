import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from backend.app import database
from backend.app.crud import create_lead, record_outreach_success
from backend.reply_sync import ReplySyncError, _raise_sync_error, parse_inbound_message, process_inbound_message


OWN_ADDRESS = "sender@example.com"
LEAD_EMAIL = "lead@example.com"
OUTBOUND_ID = "<outbound-message@example.com>"


def make_message(
    *,
    from_email=LEAD_EMAIL,
    to_email=OWN_ADDRESS,
    subject="Re: A note for Lead Co",
    body="Hi, I am interested.\n\nOn Monday sender wrote:\nIf this is not useful, reply unsubscribe and I will not follow up.",
    message_id="<inbound-message@example.com>",
    in_reply_to=OUTBOUND_ID,
    references=OUTBOUND_ID,
    html=False,
    headers=None,
):
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (headers or {}).items():
        msg[key] = value
    if html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class ReplySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database.DB_PATH = Path(self.temp_dir.name) / "reply-sync-test.db"
        database.initialize_database()
        from backend.lead_crm import initialize_crm_tables
        initialize_crm_tables()

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_sent_lead(self, email=LEAD_EMAIL, subject="A note for Lead Co"):
        lead = create_lead({"name": "Lead", "email": email, "company": "Lead Co", "industry": "Services", "niche": "SEO"})
        record_outreach_success(
            lead["id"],
            touch_type="initial",
            touch_number=0,
            subject=subject,
            body="Outbound body. If this is not useful, reply unsubscribe and I will not follow up.",
            message_id=OUTBOUND_ID,
        )
        return lead

    def reply_event_count(self):
        with database.get_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {database.EMAIL_EVENT_TABLE} WHERE status = 'reply_received'"
            ).fetchone()
        return int(row["count"])

    def test_clean_body_ignores_quoted_unsubscribe_footer(self):
        raw = make_message(body="Hi Ram,\n\nYes, I am interested. Please send details.\n\nOn Monday sender wrote:\nIf this is not useful, reply unsubscribe and I will not follow up.")
        parsed = parse_inbound_message(raw, provider_message_id="imap:INBOX:1")
        self.assertNotIn("unsubscribe", parsed.clean_body.lower())
        self.assertEqual(parsed.reply_type, "interested")

    def test_html_quote_is_removed_before_classification(self):
        raw = make_message(
            body='<div>Please send pricing.</div><div class="gmail_quote">If this is not useful, reply unsubscribe and I will not follow up.</div>',
            html=True,
        )
        parsed = parse_inbound_message(raw, provider_message_id="imap:INBOX:2")
        self.assertEqual(parsed.clean_body, "Please send pricing.")
        self.assertEqual(parsed.reply_type, "pricing_request")

    def test_common_reply_intents_are_deterministic(self):
        cases = [
            ("Please send pricing.", "pricing_request"),
            ("Can we schedule a call tomorrow?", "meeting_request"),
            ("What details do you need from me?", "question"),
            ("Not interested, thanks.", "not_interested"),
            ("Please unsubscribe me.", "unsubscribe"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                raw = make_message(body=body, message_id=f"<{expected}@example.com>")
                parsed = parse_inbound_message(raw, provider_message_id=f"imap:INBOX:{expected}")
                self.assertEqual(parsed.reply_type, expected)

    def test_network_timeout_is_reported_with_specific_code(self):
        with self.assertRaises(ReplySyncError) as raised:
            _raise_sync_error(TimeoutError("timed out"))
        self.assertEqual(raised.exception.code, "NETWORK_TIMEOUT")
        self.assertIn("TCP 993", str(raised.exception))
    def test_short_ok_reply_stays_unknown(self):
        raw = make_message(body="ok")
        parsed = parse_inbound_message(raw, provider_message_id="imap:INBOX:3")
        self.assertEqual(parsed.reply_type, "unknown")

    def test_out_of_office_classification(self):
        raw = make_message(body="I am out of office until Monday.", headers={"Auto-Submitted": "auto-replied"})
        parsed = parse_inbound_message(raw, provider_message_id="imap:INBOX:4")
        self.assertEqual(parsed.reply_type, "auto_reply")

    def test_in_reply_to_match_updates_lead_and_stores_reply_once(self):
        self.create_sent_lead()
        raw = make_message(body="Hi, I am interested. Please send more details.")
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:10", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["match_type"], "in_reply_to")
        self.assertEqual(result["reply_type"], "interested")
        self.assertEqual(self.reply_event_count(), 1)

    def test_duplicate_sync_does_not_create_second_reply_event(self):
        self.create_sent_lead()
        raw = make_message(body="Can we schedule a call?")
        first = process_inbound_message(raw, provider_message_id="imap:INBOX:11", own_addresses={OWN_ADDRESS})
        second = process_inbound_message(raw, provider_message_id="imap:INBOX:11", own_addresses={OWN_ADDRESS})
        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.reply_event_count(), 1)

    def test_own_outbound_message_is_skipped(self):
        self.create_sent_lead()
        raw = make_message(from_email=OWN_ADDRESS, to_email=LEAD_EMAIL, body="Outbound copy", message_id="<own@example.com>")
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:12", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "skipped_own")
        self.assertEqual(self.reply_event_count(), 0)

    def test_sender_subject_fallback_requires_matching_sender(self):
        self.create_sent_lead(subject="A note for Lead Co")
        good = make_message(in_reply_to="", references="", subject="Re: A note for Lead Co", body="Please send pricing.")
        bad = make_message(from_email="other@example.com", in_reply_to="", references="", subject="Re: A note for Lead Co", body="Please send pricing.", message_id="<other@example.com>")
        good_result = process_inbound_message(good, provider_message_id="imap:INBOX:13", own_addresses={OWN_ADDRESS})
        bad_result = process_inbound_message(bad, provider_message_id="imap:INBOX:14", own_addresses={OWN_ADDRESS})
        self.assertEqual(good_result["status"], "updated")
        self.assertEqual(good_result["match_type"], "sender_subject_fallback")
        self.assertEqual(bad_result["status"], "skipped")
        self.assertEqual(bad_result["reason"], "unmatched")
        self.assertEqual(self.reply_event_count(), 1)

    def test_in_reply_to_that_never_matches_sent_message_is_not_attached_to_lead(self):
        # Root cause of the "ok" bug: a message with an in_reply_to pointing to an
        # email we never sent must NOT be attached via sender_subject_fallback.
        self.create_sent_lead(subject="A note for Lead Co")
        raw = make_message(
            subject="Re: A note for Lead Co",
            body="ok",
            in_reply_to="<never-sent-message@example.com>",
            references="<never-sent-message@example.com>",
            message_id="<false-ok@example.com>",
        )
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:20", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "unmatched")
        self.assertEqual(self.reply_event_count(), 0)

    def test_in_reply_to_takes_priority_over_sender_subject_fallback(self):
        # When in_reply_to points to our sent message, we must use in_reply_to match,
        # never the fallback (even if the subject also matches).
        self.create_sent_lead(subject="A note for Lead Co")
        raw = make_message(
            subject="Re: A note for Lead Co",
            body="Yes I am interested.",
            in_reply_to=OUTBOUND_ID,
            references=OUTBOUND_ID,
            message_id="<proper-inreply@example.com>",
        )
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:21", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["match_type"], "in_reply_to")
        self.assertEqual(result["reply_type"], "interested")
        self.assertEqual(self.reply_event_count(), 1)

    def test_reply_summary_stores_latest_reply_not_first(self):
        # The lead should reflect the most recent genuine reply, and we should not
        # let an earlier/lower-confidence reply override later accurate data.
        self.create_sent_lead(subject="A note for Lead Co")
        first = make_message(body="ok", message_id="<ok-1@example.com>", in_reply_to=OUTBOUND_ID)
        second = make_message(body="Not interested. Thanks.", message_id="<ni-1@example.com>", in_reply_to=OUTBOUND_ID)
        r1 = process_inbound_message(first, provider_message_id="imap:INBOX:30", own_addresses={OWN_ADDRESS})
        r2 = process_inbound_message(second, provider_message_id="imap:INBOX:31", own_addresses={OWN_ADDRESS})
        self.assertEqual(r1["status"], "updated")
        self.assertEqual(r2["status"], "updated")
        with database.get_connection() as conn:
            lead = conn.execute("SELECT reply_type, reply_summary FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
        self.assertEqual(lead["reply_type"], "not_interested")
        self.assertEqual(lead["reply_summary"], "Not interested. Thanks.")

    def test_not_interested_classification(self):
        raw = make_message(body="Not interested. Please don't follow up.")
        parsed = parse_inbound_message(raw, provider_message_id="imap:INBOX:40")
        self.assertEqual(parsed.reply_type, "not_interested")
        self.assertIn("Not interested", parsed.clean_body)

    def test_not_interested_sets_lead_status_to_not_interested_and_timeline_event(self):
        self.create_sent_lead()
        raw = make_message(body="Not interested. Please don't follow up.", in_reply_to=OUTBOUND_ID)
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:41", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["reply_type"], "not_interested")
        with database.get_connection() as conn:
            lead = conn.execute("SELECT last_status, reply_type, reply_summary, reply_received_at, opt_out_at FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
            self.assertEqual(lead["last_status"], "not_interested")
            self.assertEqual(lead["reply_type"], "not_interested")
            self.assertEqual(lead["reply_summary"], "Not interested. Please don't follow up.")
            self.assertIsNotNone(lead["opt_out_at"])
        # Timeline should expose exactly one REPLY_RECEIVED event with matching status
        from backend.lead_crm import get_lead_timeline
        timeline = get_lead_timeline(self._lead_id())
        reply_events = [e for e in timeline if e["event_type"] == "REPLY_RECEIVED"]
        self.assertEqual(len(reply_events), 1)
        self.assertEqual(reply_events[0]["status"], "not_interested")
        self.assertEqual(reply_events[0]["reply_type"], "not_interested")
        self.assertIn("Not interested", reply_events[0]["body"])

    def test_unsubscribe_sets_lead_status_to_unsubscribed(self):
        self.create_sent_lead()
        raw = make_message(body="Please unsubscribe me.", in_reply_to=OUTBOUND_ID)
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:42", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["reply_type"], "unsubscribe")
        with database.get_connection() as conn:
            lead = conn.execute("SELECT last_status, reply_type, opt_out_at FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
            self.assertEqual(lead["last_status"], "unsubscribed")
            self.assertIsNotNone(lead["opt_out_at"])

    def test_positive_reply_sets_lead_status_to_replied(self):
        self.create_sent_lead()
        raw = make_message(body="Yes, I am interested. Please send details.", in_reply_to=OUTBOUND_ID)
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:43", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["reply_type"], "interested")
        with database.get_connection() as conn:
            lead = conn.execute("SELECT last_status FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
            self.assertEqual(lead["last_status"], "replied")

    def _lead_id(self):
        with database.get_connection() as conn:
            row = conn.execute("SELECT id FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
        return int(row["id"]) if row else None

    def test_reply_received_at_is_after_sent_at_ordering(self):
        # Regression: a skewed sender Date header must not make the reply appear
        # to happen before the outbound email was sent.
        self.create_sent_lead()
        raw = make_message(body="OK sounds good.", in_reply_to=OUTBOUND_ID)
        result = process_inbound_message(raw, provider_message_id="imap:INBOX:44", own_addresses={OWN_ADDRESS})
        self.assertEqual(result["status"], "updated")
        with database.get_connection() as conn:
            lead = conn.execute("SELECT sent_at, reply_received_at FROM app_lead WHERE email = ?", (LEAD_EMAIL,)).fetchone()
            self.assertGreaterEqual(lead["reply_received_at"], lead["sent_at"])

    def test_duplicate_provider_message_id_never_creates_second_reply(self):
        self.create_sent_lead()
        raw = make_message(body="Hello!", in_reply_to=OUTBOUND_ID)
        first = process_inbound_message(raw, provider_message_id="imap:INBOX:50", own_addresses={OWN_ADDRESS})
        self.assertEqual(first["status"], "updated")
        # Re-run with the same provider_message_id but different in-message Message-ID
        # should be detected as duplicate because provider_message_id matches.
        dup_raw = make_message(body="Hello!", in_reply_to=OUTBOUND_ID, message_id="<renamed@example.com>")
        second = process_inbound_message(dup_raw, provider_message_id="imap:INBOX:50", own_addresses={OWN_ADDRESS})
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.reply_event_count(), 1)


if __name__ == "__main__":
    unittest.main()