import sqlite3
conn = sqlite3.connect(r'leads.db', timeout=10)
conn.row_factory = sqlite3.Row

print('=== LEADS WITH REPLIES ===')
rows = conn.execute('SELECT id, name, email, last_status, reply_type, reply_summary, reply_received_at, thread_subject, last_message_id, sent_at, opt_out_at, sequence_status FROM app_lead WHERE reply_received_at IS NOT NULL').fetchall()
for r in rows:
    d = dict(r)
    for k, v in d.items():
        print(f'  {k}: {v}')
    print()

print('=== ALL EMAIL EVENTS FOR LEAD 470 ===')
rows = conn.execute('SELECT * FROM app_lead_email_event WHERE lead_id = 470 ORDER BY id').fetchall()
for r in rows:
    d = dict(r)
    print(f'  Event ID={d["id"]} type={d["touch_type"]} status={d["status"]}')
    print(f'    body_preview={d["body_preview"]!r}')
    print(f'    clean_body={d["clean_body"]!r}')
    print(f'    message_id={d["message_id"]}')
    print(f'    in_reply_to={d["in_reply_to"]}')
    print(f'    from_email={d["from_email"]}')
    print(f'    match_type={d["match_type"]}')
    print(f'    reply_type={d["reply_type"]}')
    print(f'    reply_summary={d["reply_summary"]}')
    print(f'    provider={d["provider"]}')
    print(f'    provider_message_id={d["provider_message_id"]}')
    print(f'    sent_at={d["sent_at"]}')
    print(f'    created_at={d["created_at"]}')
    print(f'    processed_at={d["processed_at"]}')
    print()

print('=== REPLY in_reply_to vs SENT message_id ===')
sent_ids = set()
rows = conn.execute('SELECT id, message_id FROM app_lead_email_event WHERE lead_id = 470 AND status = "sent"').fetchall()
for r in rows:
    sent_ids.add(r['message_id'])
    print(f'  Sent event {r["id"]}: message_id={r["message_id"]}')

rows = conn.execute('SELECT id, in_reply_to, message_id FROM app_lead_email_event WHERE lead_id = 470 AND status = "reply_received"').fetchall()
for r in rows:
    match = "YES" if r['in_reply_to'] in sent_ids else "NO"
    print(f'  Reply event {r["id"]}: in_reply_to={r["in_reply_to"]} -> matches sent? {match}')

print()
print('=== ALL LEADS COUNT ===')
r = conn.execute('SELECT COUNT(*) as cnt FROM app_lead').fetchone()
print(f'Total leads: {r["cnt"]}')

print()
print('=== ALL SENT EVENTS FOR THIS LEAD ===')
rows = conn.execute('SELECT id, touch_type, subject, body_preview, status, message_id, reply_type, reply_summary FROM app_lead_email_event WHERE lead_id = 470 ORDER BY id').fetchall()
for r in rows:
    d = dict(r)
    print(f'  ID={d["id"]} type={d["touch_type"]} status={d["status"]} subject={d["subject"]!r} body={d["body_preview"][:80]!r} reply_type={d["reply_type"]!r} reply_summary={d["reply_summary"]!r}')

print()
print('=== TIMELINE QUERY RESULT ===')
rows = conn.execute('''
    SELECT
        'email_event' AS event_type,
        touch_type AS subtype,
        subject,
        body_preview AS description,
        COALESCE(sent_at, created_at) AS occurred_at,
        status,
        open_count,
        reply_type,
        reply_summary,
        error
    FROM app_lead_email_event
    WHERE lead_id = 470
''').fetchall()
for r in rows:
    d = dict(r)
    print(f'  type={d["event_type"]} subtype={d["subtype"]} title={d["subject"]!r} desc={d["description"][:80]!r} occurred_at={d["occurred_at"]} status={d["status"]} reply_type={d["reply_type"]}')

conn.close()
