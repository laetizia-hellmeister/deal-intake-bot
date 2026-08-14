"""Daily two-way digest of the Outreach-chase funnel, posted to Slack.

Phase 1 (process_pending_replies, in outreach_replies.py): apply any of
Laetizia's thread replies to prior digests to Attio — advancing a deal's
stage, logging a chase, or passing it — before building the next digest.
A failure here is logged and reported but never blocks Phase 2.

Phase 2 (this file): scan the Deal Pipeline for entries due for their next
outreach action (Follow Up 1, Follow Up 2, Partner Attempt/Warm Intro, or
"consider passing" if stuck at Partner Attempt/Warm Intro 7+ days), and
post a single Slack message grouped by Deal Lead (so colleagues still get
@-mentioned about their own stale deals), then by action-type within each
lead's section. Runs daily; hour-window gating is deliberately loose (GitHub
Actions cron drifts by 1-2 hours on busy days) — we gate on same-day
Slack-history dedupe instead: if we haven't already posted today's digest,
post.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from attio_client import AttioClient, AttioError
from config import (
    ATTIO_MEMBER_TO_SLACK_USER,
    DEAL_PIPELINE_LIST_ID,
    DIGEST_MARKER,
    OUTREACH_INITIAL_DAYS,
    PARENT_OBJECT,
    PARTNER_NUDGE_DAYS,
    STAGE_FOLLOW_UP_1,
    STAGE_FOLLOW_UP_2,
    STAGE_OUTREACH,
    STAGE_PARTNER_WARM_INTRO,
)
from outreach_replies import process_pending_replies
from slack_client import SlackClient

_SECTION_ORDER = [
    ("due_fu1", "Due for Follow Up 1"),
    ("due_fu2", "Due for Follow Up 2"),
    ("due_partner", "Due for Partner Attempt/Warm Intro"),
    (
        "consider_passing",
        "⚠️ 7+ days at Partner Attempt/Warm Intro — consider passing",
    ),
]


def main() -> int:
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))

    slack = SlackClient()
    attio = AttioClient()

    # Phase 1 — apply pending replies. Runs every fire (daily), never
    # weekday-gated, and never blocks Phase 2 below.
    try:
        process_pending_replies(slack, attio)
    except Exception as e:
        print(f"[replies] phase failed, continuing to digest: {e}")
        traceback.print_exc()
        try:
            slack.post_message(f"⚠️ Outreach reply-processing crashed: {_short(e)}")
        except Exception:
            pass

    # Phase 2 — digest posting.
    if not is_manual and _digest_already_posted_today(slack, now_local):
        print("Skipping — outreach digest already posted today")
        attio.close()
        return 0

    try:
        buckets = _scan_pipeline_entries(attio)
    except Exception as e:
        print(f"Failed to scan Deal Pipeline entries: {e}")
        attio.close()
        return 1

    text = _format_digest(buckets, attio, now_local)
    if not text:
        print("No due Outreach-funnel deals — skipping post")
        attio.close()
        return 0

    try:
        slack.post_message(text)
    except Exception as e:
        print(f"Failed to post outreach digest: {e}")
        attio.close()
        return 1

    print("Posted Outreach chase digest.")
    attio.close()
    return 0


# ---------------------------------------------------------------------
# Querying + bucketing
# ---------------------------------------------------------------------

def _scan_pipeline_entries(attio: AttioClient) -> dict[str, list[dict]]:
    """One paginated pass over the Deal Pipeline list, bucketed by which
    digest section (if any) an entry belongs in."""
    now = datetime.now(timezone.utc)
    buckets: dict[str, list[dict]] = {key: [] for key, _ in _SECTION_ORDER}
    offset = 0
    PAGE_SIZE = 500
    MAX_SCAN = 50_000
    scanned = 0
    while scanned < MAX_SCAN:
        try:
            page = attio.query_list_entries(
                DEAL_PIPELINE_LIST_ID, filter_=None, limit=PAGE_SIZE, offset=offset
            )
        except AttioError as e:
            print(f"[chase] failed page at offset {offset}: {e}")
            break
        if not page:
            break
        for entry in page:
            _bucket_entry(entry, buckets, now)
        scanned += len(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return buckets


def _bucket_entry(entry: dict, buckets: dict[str, list[dict]], now: datetime) -> None:
    stage = AttioClient.entry_status_value(entry, "stage")
    if stage == STAGE_OUTREACH:
        if AttioClient.entry_date_value(entry, "last_chased") is not None:
            return  # already chased once but stage wasn't advanced — leave alone
        created = AttioClient.entry_created_at(entry)
        if created and (now - created).days >= OUTREACH_INITIAL_DAYS:
            buckets["due_fu1"].append(entry)
    elif stage == STAGE_FOLLOW_UP_1:
        rd = AttioClient.entry_date_value(entry, "review_date")
        if rd is None or rd <= now.date():
            buckets["due_fu2"].append(entry)
    elif stage == STAGE_FOLLOW_UP_2:
        rd = AttioClient.entry_date_value(entry, "review_date")
        if rd is None or rd <= now.date():
            buckets["due_partner"].append(entry)
    elif stage == STAGE_PARTNER_WARM_INTRO:
        anchor = AttioClient.entry_date_value(entry, "last_chased")
        if anchor is None:
            created = AttioClient.entry_created_at(entry)
            anchor = created.date() if created else None
        if anchor and (now.date() - anchor).days >= PARTNER_NUDGE_DAYS:
            buckets["consider_passing"].append(entry)


def _group_by_first_deal_lead(entries: list[dict]) -> dict[str | None, list[dict]]:
    """Group entries by their first Deal Lead's Attio member id."""
    grouped: dict[str | None, list[dict]] = defaultdict(list)
    for entry in entries:
        ev = entry.get("entry_values") or {}
        leads = ev.get("deal_lead") or []
        lead_id: str | None = None
        if leads and isinstance(leads, list) and isinstance(leads[0], dict):
            first = leads[0]
            lead_id = first.get("referenced_actor_id") or (
                first.get("actor") or {}
            ).get("id")
        grouped[lead_id].append(entry)
    return grouped


# ---------------------------------------------------------------------
# Enrichment + formatting
# ---------------------------------------------------------------------

def _enrich_entry(attio: AttioClient, entry: dict, now: datetime) -> dict:
    company_id = AttioClient.parent_record_id(entry)
    company_name = "unknown"
    person_name = None
    linkedin_url = None
    if company_id:
        record = attio.get_record(PARENT_OBJECT, company_id)
        if record:
            company_name = AttioClient.company_name(record) or f"company:{company_id[:8]}"
            team_ids = AttioClient.company_team_ids(record)
            if team_ids:
                person = attio.get_record("people", next(iter(team_ids)))
                if person:
                    person_name = AttioClient.person_name(person)
                    linkedin_url = AttioClient.person_linkedin(person)
        else:
            company_name = f"company:{company_id[:8]}"
    created = AttioClient.entry_created_at(entry)
    days = (now - created).days if created else None
    next_steps = AttioClient.entry_text_value(entry, "next_steps")
    return {
        "entry_id": AttioClient.entry_id(entry),
        "company": company_name,
        "person": person_name,
        "linkedin": linkedin_url,
        "days": days,
        "next_steps": next_steps if next_steps and len(next_steps) <= 80 else None,
    }


def _format_digest(
    buckets: dict[str, list[dict]], attio: AttioClient, now_local: datetime
) -> str:
    now_utc = datetime.now(timezone.utc)

    # Invert to {lead_id: {bucket_key: [entries]}}, preserving section order.
    by_lead: dict[str | None, dict[str, list[dict]]] = defaultdict(dict)
    total_by_lead: dict[str | None, int] = defaultdict(int)
    for key, _ in _SECTION_ORDER:
        for lead_id, entries in _group_by_first_deal_lead(buckets.get(key) or []).items():
            by_lead[lead_id][key] = entries
            total_by_lead[lead_id] += len(entries)

    if not by_lead:
        return ""

    when = "morning" if now_local.hour < 14 else "afternoon"
    lines = [f"{DIGEST_MARKER} ({when} chase)"]
    lead_order = sorted(by_lead.keys(), key=lambda lid: (lid is None, -total_by_lead[lid]))
    counter = 1
    for lead_id in lead_order:
        if lead_id is None:
            mention = "_unassigned_"
        else:
            slack_uid = ATTIO_MEMBER_TO_SLACK_USER.get(lead_id)
            mention = f"<@{slack_uid}>" if slack_uid else "_unmapped_"
        lines.append("")
        lines.append(mention)
        for key, title in _SECTION_ORDER:
            entries = by_lead[lead_id].get(key) or []
            if not entries:
                continue
            enriched = [_enrich_entry(attio, e, now_utc) for e in entries]
            enriched.sort(key=lambda x: -(x["days"] or 0))
            lines.append(f"*{title}*")
            for row in enriched:
                lines.append(_format_row(counter, row))
                if row["next_steps"]:
                    lines.append(f"   _{row['next_steps']}_")
                counter += 1

    lines.append("")
    lines.append('Reply in this thread, e.g. "1 followed up 2 pass 3 skip"')
    return "\n".join(lines)


def _format_row(num: int, row: dict) -> str:
    header = f"*{row['company']}*"
    if row["person"]:
        header += f" — {row['person']}"
    bits = [header]
    if row["days"] is not None:
        bits.append(f"{row['days']}d waiting")
    if row["linkedin"]:
        bits.append(f"<{row['linkedin']}|LinkedIn ↗>")
    line = f"{num}. " + " · ".join(bits)
    return f"{line}  `id:{row['entry_id']}`"


# ---------------------------------------------------------------------
# Slack-history dedupe
# ---------------------------------------------------------------------

def _digest_already_posted_today(slack: SlackClient, now_local: datetime) -> bool:
    """True if the bot already posted today's outreach-chase digest."""
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_today = int((now_local - today_start).total_seconds()) + 60
    try:
        messages = slack.fetch_recent_messages(
            lookback_seconds=seconds_today, limit=200
        )
    except Exception as e:
        print(f"[chase-dedupe] couldn't check history: {e}")
        return False  # fail open — better to post a duplicate than skip silently
    for msg in messages:
        if not (msg.get("bot_id") or msg.get("subtype") == "bot_message"):
            continue
        text = msg.get("text") or ""
        if DIGEST_MARKER in text:
            return True
    return False


def _short(e: Exception, n: int = 160) -> str:
    s = str(e)
    return s if len(s) <= n else s[: n - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
