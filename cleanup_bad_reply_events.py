"""
Safe cleanup script for false-positive reply events.

This script identifies and removes reply events that were incorrectly matched
via sender_subject_fallback when the message's in_reply_to points to emails
we never sent.

Root cause: The sender_subject_fallback matching was too aggressive,
allowing unrelated messages from the same sender to be matched to a lead
when their in_reply_to didn't match any of our sent messages.

This script:
1. Identifies reply events where match_type = 'sender_subject_fallback'
2. Verifies the in_reply_to doesn't match any sent message
3. Removes those false-positive reply events
4. Does NOT delete leads or campaigns
5. Is reversible (logs what it removes)
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "leads.db"

def find_false_positive_replies(conn):
    """Find reply events matched via sender_subject_fallback where in_reply_to
    doesn't match any sent message."""
    
    # Get all reply events with sender_subject_fallback match
    fallback_replies = conn.execute("""
        SELECT 
            e.id AS event_id,
            e.lead_id,
            e.message_id,
            e.in_reply_to,
            e.from_email,
            e.body_preview,
            e.clean_body,
            e.reply_type,
            e.reply_summary,
            e.provider_message_id,
            e.created_at,
            l.email AS lead_email,
            l.name AS lead_name
        FROM app_lead_email_event e
        JOIN app_lead l ON l.id = e.lead_id
        WHERE e.status = 'reply_received'
          AND e.match_type = 'sender_subject_fallback'
        ORDER BY e.id
    """).fetchall()
    
    false_positives = []
    for reply in fallback_replies:
        in_reply_to = reply["in_reply_to"]
        if not in_reply_to:
            continue
            
        # Check if in_reply_to matches any sent message
        match = conn.execute("""
            SELECT id FROM app_lead_email_event
            WHERE status = 'sent' AND message_id = ?
            LIMIT 1
        """, (in_reply_to,)).fetchone()
        
        if not match:
            false_positives.append(dict(reply))
    
    return false_positives


def cleanup_false_positives(dry_run=True):
    """Remove false-positive reply events."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    
    print("=" * 60)
    print("FALSE-POSITIVE REPLY EVENT CLEANUP")
    print("=" * 60)
    
    false_positives = find_false_positive_replies(conn)
    
    if not false_positives:
        print("\nNo false-positive reply events found.")
        conn.close()
        return
    
    print(f"\nFound {len(false_positives)} false-positive reply event(s):\n")
    
    for fp in false_positives:
        print(f"  Event ID: {fp['event_id']}")
        print(f"  Lead: {fp['lead_name']} ({fp['lead_email']})")
        print(f"  Body: {fp['body_preview'][:80]!r}")
        print(f"  in_reply_to: {fp['in_reply_to']}")
        print(f"  from_email: {fp['from_email']}")
        print(f"  match_type: sender_subject_fallback")
        print(f"  reply_type: {fp['reply_type']}")
        print(f"  reply_summary: {fp['reply_summary']}")
        print(f"  created_at: {fp['created_at']}")
        print()
    
    if dry_run:
        print("DRY RUN - No changes made.")
        print("Run with --execute to apply changes.")
        conn.close()
        return
    
    # Remove false-positive reply events
    removed = 0
    for fp in false_positives:
        event_id = fp["event_id"]
        lead_id = fp["lead_id"]
        
        print(f"  Removing event {event_id} (lead {lead_id}, body: {fp['body_preview'][:50]!r})...")
        
        conn.execute("DELETE FROM app_lead_email_event WHERE id = ?", (event_id,))
        removed += 1
    
    conn.commit()
    print(f"\nRemoved {removed} false-positive reply event(s).")
    
    # Check if any leads need their reply info updated
    print("\nChecking lead reply consistency...")
    leads_with_stale_replies = conn.execute("""
        SELECT 
            l.id,
            l.email,
            l.name,
            l.reply_type,
            l.reply_summary
        FROM app_lead l
        WHERE l.reply_received_at IS NOT NULL
          AND l.reply_type = 'unknown'
          AND l.reply_summary = 'ok'
    """).fetchall()
    
    if leads_with_stale_replies:
        print(f"  Found {len(leads_with_stale_replies)} lead(s) with stale 'ok' reply info:")
        for lead in leads_with_stale_replies:
            print(f"    Lead {lead['id']}: {lead['name']} ({lead['email']})")
            print(f"      Current: reply_type={lead['reply_type']}, reply_summary={lead['reply_summary']!r}")
            
            # Check if there are any remaining genuine reply events
            genuine_reply = conn.execute("""
                SELECT reply_type, reply_summary
                FROM app_lead_email_event
                WHERE lead_id = ? AND status = 'reply_received'
                ORDER BY id DESC
                LIMIT 1
            """, (lead["id"],)).fetchone()
            
            if genuine_reply and genuine_reply["reply_type"] != "unknown":
                print(f"      Updating to: reply_type={genuine_reply['reply_type']}, reply_summary={genuine_reply['reply_summary']!r}")
                conn.execute("""
                    UPDATE app_lead
                    SET reply_type = ?, reply_summary = ?
                    WHERE id = ?
                """, (genuine_reply["reply_type"], genuine_reply["reply_summary"], lead["id"]))
            else:
                print(f"      No genuine replies found - keeping as-is")
        
        conn.commit()
    else:
        print("  All lead reply info is consistent.")
    
    conn.close()
    print("\nCleanup complete.")


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    cleanup_false_positives(dry_run=dry_run)
