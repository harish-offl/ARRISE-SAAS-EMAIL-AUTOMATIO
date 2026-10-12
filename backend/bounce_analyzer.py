"""Utility to analyze bounces and email delivery issues."""

import sys
from pathlib import Path
from datetime import datetime, timedelta

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

from backend.app.crud import list_leads
from backend.email_quality import classify_bounce


def analyze_bounces(days=7):
    """Analyze bounce patterns from last N days."""
    print(f"\n📊 Bounce Analysis (Last {days} days)")
    print("=" * 80)
    
    all_leads = list_leads()
    cutoff_date = datetime.now() - timedelta(days=days)
    
    hard_bounces = []
    soft_bounces = []
    spam_filtered = []
    failed_other = []
    skipped_invalid = []
    
    for lead in all_leads:
        # Skip if no activity in period
        if lead.sent_at:
            try:
                sent_date = datetime.fromisoformat(lead.sent_at)
                if sent_date < cutoff_date:
                    continue
            except (ValueError, AttributeError):
                pass
        
        if lead.last_status == "failed":
            bounce_type = classify_bounce(lead.last_error or "")
            error_preview = (lead.last_error or "")[:100]
            
            if bounce_type == "hard":
                hard_bounces.append((lead.email, error_preview))
            elif bounce_type == "soft":
                soft_bounces.append((lead.email, error_preview))
            elif bounce_type == "spam_filter":
                spam_filtered.append((lead.email, error_preview))
            else:
                failed_other.append((lead.email, error_preview))
        
        elif lead.last_status == "skipped":
            skipped_invalid.append((lead.email, lead.last_error or ""))
    
    print(f"🔴 Hard Bounces (Don't Retry): {len(hard_bounces)}")
    for email, error in hard_bounces[:10]:
        print(f"   • {email}: {error}")
    if len(hard_bounces) > 10:
        print(f"   ... and {len(hard_bounces) - 10} more")
    
    print(f"\n🟡 Soft Bounces (Can Retry): {len(soft_bounces)}")
    for email, error in soft_bounces[:10]:
        print(f"   • {email}: {error}")
    if len(soft_bounces) > 10:
        print(f"   ... and {len(soft_bounces) - 10} more")
    
    print(f"\n📨 Spam Filtered (In Spam Folder): {len(spam_filtered)}")
    for email, error in spam_filtered[:10]:
        print(f"   • {email}: {error}")
    if len(spam_filtered) > 10:
        print(f"   ... and {len(spam_filtered) - 10} more")
    
    print(f"\n❓ Other Failures: {len(failed_other)}")
    for email, error in failed_other[:5]:
        print(f"   • {email}: {error}")
    if len(failed_other) > 5:
        print(f"   ... and {len(failed_other) - 5} more")
    
    print(f"\n⚠️  Skipped (Invalid Address): {len(skipped_invalid)}")
    for email, reason in skipped_invalid[:10]:
        print(f"   • {email}: {reason}")
    if len(skipped_invalid) > 10:
        print(f"   ... and {len(skipped_invalid) - 10} more")
    
    print("\n" + "=" * 80)
    total_sent = sum(1 for l in all_leads if l.last_status == "sent")
    total_failed = len(hard_bounces) + len(soft_bounces) + len(spam_filtered) + len(failed_other)
    
    if total_sent > 0:
        bounce_rate = (total_failed / (total_sent + total_failed)) * 100
        print(f"\n📈 Summary:")
        print(f"   Total Sent: {total_sent}")
        print(f"   Total Failures: {total_failed}")
        print(f"   Bounce Rate: {bounce_rate:.1f}%")
        
        if bounce_rate > 5:
            print(f"\n   ⚠️  WARNING: Bounce rate is high! Review email content and list quality.")
    
    print(f"\n💡 Recommendations:")
    print(f"   1. Remove {len(hard_bounces)} hard bounce addresses from next campaign")
    print(f"   2. Review email template if spam filter rate is > 5%")
    print(f"   3. Validate {len(skipped_invalid)} addresses before importing")
    print(f"   4. Retry {len(soft_bounces)} soft bounces after 1-2 hours\n")


def cleanup_hard_bounces():
    """Mark hard bounces as 'opt_out' to skip in future campaigns."""
    print("\n🧹 Cleaning up hard bounces...")
    from backend.app.crud import update_lead_status
    
    all_leads = list_leads()
    cleaned = 0
    
    for lead in all_leads:
        if lead.last_status == "failed":
            bounce_type = classify_bounce(lead.last_error or "")
            if bounce_type == "hard":
                try:
                    # Mark as opted out so they're skipped
                    update_lead_status(lead.id, opt_out_at=datetime.now().isoformat())
                    cleaned += 1
                except:
                    pass
    
    print(f"   ✓ Marked {cleaned} hard bounces as opted out\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Analyze email bounces and delivery issues")
    parser.add_argument("--days", type=int, default=7, help="Analyze last N days")
    parser.add_argument("--cleanup", action="store_true", help="Mark hard bounces as opted out")
    args = parser.parse_args()
    
    analyze_bounces(args.days)
    
    if args.cleanup:
        cleanup_hard_bounces()
