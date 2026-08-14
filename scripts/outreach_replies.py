"""Phase 1 of the outreach-chase digest: process Laetizia's thread replies
to prior digests and apply them to Attio, before the next digest is posted.

Row numbers in a digest are resolved back to a specific Attio list entry
via a full entry_id UUID embedded as an inline-code tag on each digest row
(see outreach_chase.py's _format_row). This repo is deliberately stateless
(ingest.py's dedupe already relies purely on Slack history + reactions), so
rather than a persisted state file, the digest message's own text — which
Slack keeps forever — is the source of truth for row -> entry_id.

Every reply re-reads the entry's LIVE current stage from Attio before
deciding what to do, rather than trusting whatever stage the original
digest assumed. That's what makes replying late (even to an older,
superseded digest thread) still produce the correct result.
"""

from __future__ import annotations

import re
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from attio_client import AttioClient
from config import (
    DEAL_PIPELINE_LIST_ID,
    DIGEST_MARKER,
    FOLLOW_UP_CADENCE_DAYS,
    OUTREACH_FUNNEL_STAGES,
    PARENT_OBJECT,
    REACTION_REPLY_APPLIED,
    REACTION_REPLY_CLARIFY,
    REACTION_REPLY_ERROR,
    REACTION_REPLY_SKIPPED,
    REPLY_PROCESSED_REACTIONS,
    STAGE_FOLLOW_UP_1,
    STAGE_FOLLOW_UP_2,
    STAGE_OUTREACH,
    STAGE_PARTNER_WARM_INTRO,
    STAGE_TO_PASS,
)
from reply_parser import (
    ACTION_AMBIGUOUS,
    ACTION_FOLLOWED_UP,
    ACTION_PARTNER,
    ACTION_PASS,
    ACTION_SKIP,
    ReplyItem,
    parse_reply,
)
from slack_client import SlackClient

_THREAD_LOOKBACK_DAYS = 14  # comfortably covers a few missed daily cycles

# Matches a digest row like `3. *Acme* — Jane · 8d waiting  `id:UUID`` and
# pulls out (row_number, entry_id). Everything between the row number and
# the id tag is irrelevant to this regex.
_ROW_TAG_RE = re.compile(
    r"^(\d+)\..*`id:([0-9a-fA-F-]{36})`", re.MULTILINE
)

KIND_APPLIED = "applied"
KIND_SKIPPED = "skipped"
KIND_CLARIFY = "clarify"
KIND_ERROR = "error"

_REACTION_BY_KIND = {
    KIND_APPLIED: REACTION_REPLY_APPLIED,
    KIND_SKIPPED: REACTION_REPLY_SKIPPED,
    KIND_CLARIFY: REACTION_REPLY_CLARIFY,
    KIND_ERROR: REACTION_REPLY_ERROR,
}
# Priority when a single reply mixes item kinds — worst/most-informative wins.
_KIND_PRIORITY = [KIND_ERROR, KIND_APPLIED, KIND_CLARIFY, KIND_SKIPPED]


class ApplyResult:
    def __init__(self, item: ReplyItem, kind: str, line: str):
        self.item = item
        self.kind = kind
        self.line = line


def process_pending_replies(slack: SlackClient, attio: AttioClient) -> None:
    for thread in _find_digest_threads(slack):
        row_map = _row_to_entry_id(thread.get("text") or "")
        if not row_map:
            continue
        thread_ts = thread["ts"]
        try:
            messages = slack.fetch_thread_replies(thread_ts)
        except Exception as e:
            print(f"[replies] couldn't fetch thread {thread_ts}: {e}")
            continue
        for msg in messages:
            if msg.get("ts") == thread_ts:
                continue  # the digest root itself, not a reply
            if SlackClient.is_from_bot(msg):
                continue
            if SlackClient.has_processed_reaction(msg, REPLY_PROCESSED_REACTIONS):
                continue
            _process_reply(slack, attio, msg, row_map)


def _find_digest_threads(
    slack: SlackClient, lookback_days: int = _THREAD_LOOKBACK_DAYS
) -> list[dict]:
    messages = slack.fetch_recent_messages(
        lookback_seconds=lookback_days * 86400, limit=500
    )
    threads = [
        m
        for m in messages
        if SlackClient.is_from_bot(m) and DIGEST_MARKER in (m.get("text") or "")
    ]
    threads.sort(key=lambda m: float(m.get("ts", "0")))
    return threads


def _row_to_entry_id(digest_text: str) -> dict[int, str]:
    return {int(row): entry_id for row, entry_id in _ROW_TAG_RE.findall(digest_text)}


def _process_reply(
    slack: SlackClient, attio: AttioClient, reply: dict, row_map: dict[int, str]
) -> None:
    ts = reply.get("ts")
    text = reply.get("text") or ""
    items = parse_reply(text)
    if not items:
        return  # doesn't look like a reply to the digest at all — leave untouched

    today = datetime.now(ZoneInfo("Europe/Copenhagen")).date()
    results: list[ApplyResult] = []
    for item in items:
        entry_id = row_map.get(item.row)
        if entry_id is None:
            results.append(
                ApplyResult(
                    item,
                    KIND_CLARIFY,
                    f"Row {item.row}: I don't see that row in this digest — "
                    f"reply again with the right row number.",
                )
            )
            continue
        try:
            results.append(_apply_reply_item(attio, entry_id, item, today))
        except Exception as e:
            traceback.print_exc()
            results.append(
                ApplyResult(item, KIND_ERROR, f"Row {item.row}: error — {_short(e)}")
            )

    reply_text = "\n".join(f"• {r.line}" for r in results)
    try:
        slack.post_thread_reply(ts, reply_text)
    except Exception as e:
        print(f"[replies] couldn't post confirmation for {ts}: {e}")

    try:
        slack.add_reaction(ts, _aggregate_reaction(results))
    except Exception as e:
        print(f"[replies] couldn't react to {ts}: {e}")


def _apply_reply_item(
    attio: AttioClient, entry_id: str, item: ReplyItem, today: date
) -> ApplyResult:
    entry = attio.get_list_entry_by_id(DEAL_PIPELINE_LIST_ID, entry_id)
    if entry is None:
        return ApplyResult(
            item, KIND_ERROR, f"Row {item.row}: entry no longer exists in Attio"
        )

    company_name = _company_name_for_entry(attio, entry)
    current_stage = AttioClient.entry_status_value(entry, "stage")

    if item.action == ACTION_AMBIGUOUS:
        return ApplyResult(
            item,
            KIND_CLARIFY,
            f'Row {item.row} ({company_name}): didn\'t understand "{item.raw_phrase}" '
            f"— reply again with followed up / partner / pass / skip.",
        )

    if item.action == ACTION_SKIP:
        return ApplyResult(
            item,
            KIND_SKIPPED,
            f"Row {item.row} ({company_name}): skipped, will resurface when due again.",
        )

    if item.action == ACTION_PASS:
        attio.update_list_entry(
            DEAL_PIPELINE_LIST_ID, entry_id, {"stage": STAGE_TO_PASS}
        )
        return ApplyResult(
            item, KIND_APPLIED, f"Row {item.row} ({company_name}): → To Pass."
        )

    if current_stage not in OUTREACH_FUNNEL_STAGES:
        return ApplyResult(
            item,
            KIND_CLARIFY,
            f"Row {item.row} ({company_name}): already at '{current_stage}', "
            f"no action taken.",
        )

    if item.action == ACTION_PARTNER:
        new_stage = STAGE_PARTNER_WARM_INTRO
        first_time = current_stage != STAGE_PARTNER_WARM_INTRO
        log_phrase = None
        if first_time:
            log_phrase = "Partner attempt / warm intro"
            if item.colleague:
                log_phrase += f" via {item.colleague}"
    elif item.action == ACTION_FOLLOWED_UP:
        if current_stage == STAGE_OUTREACH:
            new_stage, log_phrase = STAGE_FOLLOW_UP_1, "Follow Up 1 sent"
        elif current_stage == STAGE_FOLLOW_UP_1:
            new_stage, log_phrase = STAGE_FOLLOW_UP_2, "Follow Up 2 sent"
        else:
            # Capped repeat: already at Follow Up 2 or Partner Attempt/Warm
            # Intro — no further stage to advance to, just log another chase.
            new_stage, log_phrase = current_stage, None
    else:
        return ApplyResult(
            item, KIND_ERROR, f"Row {item.row} ({company_name}): unhandled action"
        )

    values: dict = {"last_chased": today.isoformat()}
    if new_stage != current_stage:
        values["stage"] = new_stage
    if new_stage in (STAGE_FOLLOW_UP_1, STAGE_FOLLOW_UP_2):
        # Keeps the digest's due-bucket logic coherent: without bumping
        # review_date here too, a capped repeat at FU2 would immediately
        # re-show as "due" on the very next digest despite just being chased.
        values["review_date"] = (
            today + timedelta(days=FOLLOW_UP_CADENCE_DAYS)
        ).isoformat()

    next_steps_update = _build_next_steps_update(entry, log_phrase, today)
    if next_steps_update is not None:
        values["next_steps"] = next_steps_update

    attio.update_list_entry(DEAL_PIPELINE_LIST_ID, entry_id, values)
    return ApplyResult(
        item,
        KIND_APPLIED,
        f"Row {item.row} ({company_name}): {current_stage} → {new_stage}.",
    )


_COUNTER_RE = re.compile(r"Followed up (\d+)x, last (\d{1,2} \w+ \d{4})")


def _build_next_steps_update(
    entry: dict, log_phrase: str | None, today: date
) -> str | None:
    current = AttioClient.entry_text_value(entry, "next_steps") or ""
    date_str = today.strftime("%-d %b %Y")

    if log_phrase:
        new_line = f"{log_phrase} — {date_str}"
        return f"{current}\n{new_line}" if current else new_line

    # Capped repeat: find-and-update a single running counter line in
    # place rather than stacking a new dated line every time.
    m = _COUNTER_RE.search(current)
    if m:
        count = int(m.group(1)) + 1
        return _COUNTER_RE.sub(f"Followed up {count}x, last {date_str}", current, count=1)
    new_line = f"Followed up 1x, last {date_str}"
    return f"{current}\n{new_line}" if current else new_line


def _company_name_for_entry(attio: AttioClient, entry: dict) -> str:
    company_id = AttioClient.parent_record_id(entry)
    if not company_id:
        return "unknown company"
    record = attio.get_record(PARENT_OBJECT, company_id)
    if record:
        return AttioClient.company_name(record) or f"company:{company_id[:8]}"
    return f"company:{company_id[:8]}"


def _aggregate_reaction(results: list[ApplyResult]) -> str:
    kinds = {r.kind for r in results}
    for kind in _KIND_PRIORITY:
        if kind in kinds:
            return _REACTION_BY_KIND[kind]
    return REACTION_REPLY_CLARIFY


def _short(e: Exception, n: int = 160) -> str:
    s = str(e)
    return s if len(s) <= n else s[: n - 1] + "…"
