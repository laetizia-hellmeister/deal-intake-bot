"""Daily digest of New Inbound Deals, posted to Slack at 09:00 Europe/Copenhagen.

Mon-Fri only. Posts a single message in #deal-intake summarising how many
deals are waiting for review, grouped by the first Deal Lead on each entry
and @-tagging that person so they get a notification.

Like promote.py, this gates inside the script on `now_local.hour == 9` so
that the two UTC schedules (07:00 / 08:00, covering CEST / CET) only do
work at the right local time.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from attio_client import AttioClient, AttioError
from config import (
    ATTIO_MEMBER_TO_SLACK_USER,
    INBOUND_DEALS_LIST_ID,
    STEP_NEW,
)
from slack_client import SlackClient


def main() -> int:
    now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))
    if now_local.hour != 9:
        print(f"Skipping — local hour is {now_local.hour}, not 9")
        return 0
    if now_local.weekday() >= 5:
        # Saturday=5, Sunday=6
        print(f"Skipping — local weekday is {now_local.weekday()} (weekend)")
        return 0

    attio = AttioClient()
    slack = SlackClient()

    try:
        entries = attio.query_list_entries(
            INBOUND_DEALS_LIST_ID,
            filter_={"step": STEP_NEW},
            limit=200,
        )
    except AttioError as e:
        print(f"Failed to fetch Inbound Deals: {e}")
        attio.close()
        return 1

    counts = _count_by_first_lead(entries)
    if not counts:
        print("No New deals — skipping post")
        attio.close()
        return 0

    text = _format_digest(counts)
    try:
        slack.post_message(text)
    except Exception as e:
        print(f"Failed to post digest: {e}")
        attio.close()
        return 1
    print(f"Posted digest covering {sum(counts.values())} deal(s) "
          f"across {len(counts)} bucket(s).")
    attio.close()
    return 0


def _count_by_first_lead(entries: list[dict]) -> Counter:
    """Count entries by their first deal_lead's Attio member id.

    Entries with no deal_lead bucket under the special key None
    (rendered as 'unassigned')."""
    counts: Counter = Counter()
    for entry in entries:
        member_id = _first_deal_lead_id(entry)
        counts[member_id] += 1
    return counts


def _first_deal_lead_id(entry: dict) -> str | None:
    ev = entry.get("entry_values") or {}
    leads = ev.get("deal_lead") or []
    if not leads or not isinstance(leads, list):
        return None
    first = leads[0]
    if not isinstance(first, dict):
        return None
    # Attio actor-reference values come back in a couple of shapes
    # depending on the response surface. Cover both.
    direct = first.get("referenced_actor_id")
    if direct:
        return direct
    nested = (first.get("actor") or {}).get("id")
    return nested


def _format_digest(counts: Counter) -> str:
    """Build the Slack message body."""
    lines = ["🌅 *New deals waiting for review:*"]
    # Sort: highest count first; tie-breaker by member_id string for stable
    # ordering. Unassigned bucket goes last regardless of count.
    items = sorted(
        counts.items(),
        key=lambda kv: (kv[0] is None, -kv[1], kv[0] or ""),
    )
    for member_id, count in items:
        plural = "s" if count != 1 else ""
        if member_id is None:
            mention = "_unassigned_"
        else:
            slack_uid = ATTIO_MEMBER_TO_SLACK_USER.get(member_id)
            mention = f"<@{slack_uid}>" if slack_uid else "_unmapped_"
        lines.append(f"• {mention} — {count} deal{plural} waiting")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
