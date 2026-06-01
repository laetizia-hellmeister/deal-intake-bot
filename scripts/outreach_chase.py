"""Twice-weekly digest of stale Outreach deals, posted to Slack.

Runs Monday evening (17:00 Europe/Copenhagen) and Thursday morning
(09:00 local). Finds Deal Pipeline entries with status = "Outreach"
that were added more than OUTREACH_STALE_DAYS calendar days ago and
groups them by first Deal Lead. Posts a single Slack message in
the bot's channel @-mentioning each lead with their stale list.

Hour-window gating is deliberately loose — GitHub Actions cron
drifts by 1-2 hours on busy days. We gate on weekday + Slack-history
dedupe instead: if it's Mon or Thu and we haven't already posted
today's digest, post.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from attio_client import AttioClient, AttioError
from config import (
    ATTIO_MEMBER_TO_SLACK_USER,
    DEAL_PIPELINE_LIST_ID,
    PARENT_OBJECT,
)
from slack_client import SlackClient

OUTREACH_STAGE = "Outreach"
# Minimum calendar days in Outreach before a deal shows up in the digest.
# Deals at exactly this many days are shown in the first bucket; the bucket
# boundary is hard-coded in _format_digest (6-10 vs >10).
OUTREACH_STALE_DAYS = 6
DIGEST_MARKER = "🐢 Outreach follow-ups"


def main() -> int:
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))
    weekday = now_local.weekday()  # 0=Mon … 6=Sun

    # Mon = 0, Thu = 3. Cron-triggered runs only fire those days; manual
    # runs always go through.
    if not is_manual and weekday not in (0, 3):
        print(
            f"Skipping — weekday is {weekday} (need Mon=0 or Thu=3) "
            f"and not a manual run"
        )
        return 0

    slack = SlackClient()
    if not is_manual and _digest_already_posted_today(slack, now_local):
        print("Skipping — outreach digest already posted today")
        return 0

    attio = AttioClient()
    try:
        stale = _find_stale_outreach_entries(attio)
    except Exception as e:
        print(f"Failed to fetch stale Outreach entries: {e}")
        attio.close()
        return 1

    if not stale:
        print("No stale Outreach deals — skipping post")
        attio.close()
        return 0

    grouped = _group_by_first_deal_lead(stale)
    enriched = _enrich_with_company_names(attio, grouped)
    text = _format_digest(enriched, now_local)

    try:
        slack.post_message(text)
    except Exception as e:
        print(f"Failed to post outreach digest: {e}")
        attio.close()
        return 1

    total = sum(len(items) for items in grouped.values())
    print(
        f"Posted Outreach chase digest covering {total} stale deal(s) "
        f"across {len(grouped)} lead bucket(s)."
    )
    attio.close()
    return 0


# ---------------------------------------------------------------------
# Querying + filtering
# ---------------------------------------------------------------------

def _find_stale_outreach_entries(attio: AttioClient) -> list[dict]:
    """All Deal Pipeline entries whose Status is Outreach AND whose
    created_at is older than OUTREACH_STALE_DAYS calendar days.

    We paginate unfiltered (the bot's filter syntax doesn't reliably
    match parent_record_id; we use the same client-side filter pattern
    here for robustness) and filter on both fields locally."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=OUTREACH_STALE_DAYS)
    stale: list[dict] = []
    offset = 0
    PAGE_SIZE = 500
    MAX_SCAN = 50_000
    scanned = 0
    while scanned < MAX_SCAN:
        try:
            page = attio.query_list_entries(
                DEAL_PIPELINE_LIST_ID, filter_=None,
                limit=PAGE_SIZE, offset=offset,
            )
        except AttioError as e:
            print(f"[chase] failed page at offset {offset}: {e}")
            break
        if not page:
            break
        for entry in page:
            if _entry_stage(entry) != OUTREACH_STAGE:
                continue
            ts = _entry_created_at(entry)
            if not ts:
                continue
            if ts >= cutoff:
                continue
            stale.append(entry)
        scanned += len(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return stale


def _entry_stage(entry: dict) -> str | None:
    """Pull the Status (api_slug `stage`) title from a Pipeline entry."""
    ev = (entry or {}).get("entry_values") or {}
    raw = ev.get("stage")
    if not raw:
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    inner = raw.get("status")
    if isinstance(inner, dict):
        return inner.get("title") or inner.get("name")
    return raw.get("title") or raw.get("value")


def _entry_created_at(entry: dict) -> datetime | None:
    """Pull a UTC datetime from a list entry's created_at."""
    raw = (entry or {}).get("created_at")
    if not raw:
        raw = ((entry or {}).get("entry_values") or {}).get("created_at")
    iso = _first_text_value(raw)
    if not iso:
        return None
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _first_text_value(v) -> str | None:
    if not v:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v:
        return _first_text_value(v[0])
    if isinstance(v, dict):
        return v.get("value") or v.get("formatted")
    return None


# ---------------------------------------------------------------------
# Grouping + formatting
# ---------------------------------------------------------------------

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


def _enrich_with_company_names(
    attio: AttioClient, grouped: dict[str | None, list[dict]]
) -> dict[str | None, list[dict]]:
    """Fetch the company name + days-in-Outreach for each entry.
    Returns {lead_id: [{name, days}, ...]}. No URLs — display is tabular
    and links would break the code-block alignment."""
    out: dict[str | None, list[dict]] = {}
    now = datetime.now(timezone.utc)
    for lead_id, entries in grouped.items():
        items: list[dict] = []
        for entry in entries:
            company_id = AttioClient.parent_record_id(entry)
            name = "unknown"
            if company_id:
                record = attio.get_record(PARENT_OBJECT, company_id)
                if record:
                    name = AttioClient.company_name(record) or f"company:{company_id[:8]}"
                else:
                    name = f"company:{company_id[:8]}"
            ts = _entry_created_at(entry)
            days = (now - ts).days if ts else None
            items.append({"name": name, "days": days})
        items.sort(key=lambda x: (-(x["days"] or 0), x["name"].lower()))
        out[lead_id] = items
    return out


# Column widths for the code-block table inside each lead's section.
_NAME_COL_WIDTH = 32
_DAYS_COL_WIDTH = 8


def _format_digest(
    enriched: dict[str | None, list[dict]],
    now_local: datetime,
) -> str:
    """Build the Slack message — one section per Deal Lead with a
    monospace code-block table grouped into two staleness buckets:
      6–10 days  (gentle nudge)
      >10 days  (critical — warm intro or pass)

    Each lead's @-mention sits *outside* the code block so Slack
    notifications fire; the table itself is inside ``` fences for
    clean alignment without link clutter."""
    when = "morning" if now_local.hour < 14 else "evening"
    lines = [f"{DIGEST_MARKER} ({when} chase)"]

    # Sort buckets: most total stale-deals first; unassigned last.
    items = sorted(
        enriched.items(),
        key=lambda kv: (kv[0] is None, -len(kv[1])),
    )
    for lead_id, deals in items:
        bucket_mid = [d for d in deals if d["days"] is not None and 6 <= d["days"] <= 10]
        bucket_crit = [d for d in deals if d["days"] is not None and d["days"] > 10]
        if not bucket_mid and not bucket_crit:
            continue

        if lead_id is None:
            mention = "_unassigned_"
        else:
            slack_uid = ATTIO_MEMBER_TO_SLACK_USER.get(lead_id)
            mention = f"<@{slack_uid}>" if slack_uid else "_unmapped_"

        lines.append("")
        lines.append(mention)

        table_lines = ["```"]
        if bucket_mid:
            table_lines.append("6–10 days")
            for d in sorted(bucket_mid, key=lambda x: -x["days"]):
                table_lines.append(_format_row(d["name"], d["days"]))
        if bucket_crit:
            if bucket_mid:
                table_lines.append("")
            table_lines.append(">10 days  (critical — warm intro or pass)")
            for d in sorted(bucket_crit, key=lambda x: -x["days"]):
                table_lines.append(_format_row(d["name"], d["days"]))
        table_lines.append("```")
        lines.extend(table_lines)
    return "\n".join(lines)


def _format_row(name: str, days: int) -> str:
    """One table row, padded so the days column aligns."""
    safe_name = name if len(name) <= _NAME_COL_WIDTH else name[: _NAME_COL_WIDTH - 1] + "…"
    return f"  {safe_name.ljust(_NAME_COL_WIDTH)}{str(days).rjust(3)} days"


# ---------------------------------------------------------------------
# Slack-history dedupe
# ---------------------------------------------------------------------

def _digest_already_posted_today(
    slack: SlackClient, now_local: datetime
) -> bool:
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


if __name__ == "__main__":
    sys.exit(main())
