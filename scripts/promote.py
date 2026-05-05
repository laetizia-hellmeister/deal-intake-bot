"""Promote workflow entry point.

Runs twice daily — 12:00 + 17:30 Europe/Copenhagen — gated in-script to
handle DST and to allow manual workflow_dispatch runs at any time.
Moves Inbound Deals entries whose Step == "Add to pipeline" into the main
Deal Pipeline, then flips the Inbound Step to "Added".

SAFETY: this script MUST NOT call update/delete endpoints on Deal Pipeline.
The only Deal Pipeline endpoint used here is add-record-to-list.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from attio_client import AttioClient, AttioError
from config import (
    ATTIO_MEMBER_TO_SLACK_USER,
    DEAL_PIPELINE_LIST_ID,
    INBOUND_DEALS_LIST_ID,
    IN_SCOPE_STAGES,
    NAME_FUZZY_THRESHOLD,
    PARENT_OBJECT,
    PIPELINE_DEFAULT_STAGE,
    PIPELINE_SOURCING_CHANNELS,
    STEP_ADDED,
    STEP_DUPLICATE,
    STEP_NEW,
    STEP_NOT_RELEVANT,
    STEP_PASSED_RECENT,
)
from dedupe import extract_inbound_step, extract_pipeline_stage
from slack_client import SlackClient


def main() -> int:
    # Manual runs (workflow_dispatch) always do work — they're the
    # "test now" / "promote now" button. Cron-triggered runs go through
    # the time gate so only the schedules that map to a target window
    # for the current DST state actually run.
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))

    if not is_manual and not _in_target_window(now_local):
        print(
            f"Skipping — local time is {now_local.strftime('%H:%M')}, "
            "not within 12:00-12:59 or 17:30-18:29 window"
        )
        return 0

    attio = AttioClient()
    slack = SlackClient()

    try:
        entries = attio.inbound_deals_entries_to_promote(limit=50)
    except AttioError as e:
        print(f"Failed to fetch Inbound Deals: {e}")
        attio.close()
        return 1

    promoted: list[str] = []
    failed: list[tuple[str, str]] = []

    for entry in entries:
        entry_id = AttioClient.entry_id(entry)
        company_id = AttioClient.parent_record_id(entry)
        company_name = _resolve_company_name(attio, entry, company_id)

        if not entry_id or not company_id:
            print(f"Skipping malformed entry: {entry}")
            continue

        try:
            _promote_one(attio, company_id, entry)
        except AttioError as e:
            msg = str(e)
            if _is_already_exists(msg):
                print(f"{company_name}: already in Deal Pipeline — marking Added")
                _mark_added(attio, entry_id, failed, company_name)
                promoted.append(company_name)
                continue
            print(f"{company_name}: promotion failed: {msg}")
            failed.append((company_name, _short(msg)))
            continue
        except Exception as e:
            print(f"{company_name}: unexpected error: {e}")
            failed.append((company_name, _short(str(e))))
            continue

        _mark_added(attio, entry_id, failed, company_name)
        promoted.append(company_name)

    # Slack-posting policy:
    #  - Evening run (17:30) and manual runs: full digest with open
    #    breakdown + @-mentions. This is the daily "queue check-in".
    #    Also runs the cleanup pass that archives stale Duplicate /
    #    Passed (<100 days) entries whose parent deal is done.
    #  - Noon run: silent unless something failed. Promotions still
    #    happen — the deals just appear in Deal Pipeline without a
    #    Slack post until the evening digest counts them in the queue.
    is_evening_or_manual = is_manual or _in_evening_window(now_local)
    if is_evening_or_manual:
        open_breakdown = _open_breakdown(attio)
        _post_summary(slack, promoted, failed, open_breakdown)
        _archive_completed_duplicates(attio)
    elif failed:
        # Noon run with errors — surface them so they don't get lost.
        _post_summary(slack, [], failed, None)
    attio.close()
    return 0


def _open_breakdown(attio: AttioClient) -> Counter | None:
    """Count Inbound entries with Step=New, grouped by first deal_lead.

    Returns a Counter[str | None] keyed by Attio workspace_member_id, with
    None bucketing entries that have no deal lead. Returns None on error
    so the summary is still posted (without the breakdown).
    """
    try:
        entries = attio.query_list_entries(
            INBOUND_DEALS_LIST_ID,
            filter_={"step": STEP_NEW},
            limit=200,
        )
    except Exception as e:
        print(f"Failed to fetch open deals for breakdown: {e}")
        return None

    counts: Counter = Counter()
    for entry in entries:
        ev = entry.get("entry_values") or {}
        leads = ev.get("deal_lead") or []
        member_id: str | None = None
        if leads and isinstance(leads, list) and isinstance(leads[0], dict):
            first = leads[0]
            member_id = first.get("referenced_actor_id") or (
                first.get("actor") or {}
            ).get("id")
        counts[member_id] += 1
    return counts


def _in_target_window(now_local: datetime) -> bool:
    """True if the current local time is in either of the promote windows.

    Two daily windows:
      noon:    12:00-12:59 (cron fires at 12:00, allow up to 1h drift)
      evening: 17:30-18:29 (cron fires at 17:30, allow up to 1h drift)
    GitHub Actions cron is best-effort and can drift 5-15 min under load.
    """
    minutes = now_local.hour * 60 + now_local.minute
    return _in_noon_window_minutes(minutes) or _in_evening_window_minutes(minutes)


def _in_evening_window(now_local: datetime) -> bool:
    """True if the current local time is in the 17:30 promote window."""
    minutes = now_local.hour * 60 + now_local.minute
    return _in_evening_window_minutes(minutes)


def _in_noon_window_minutes(minutes: int) -> bool:
    return (12 * 60) <= minutes < (13 * 60)


def _in_evening_window_minutes(minutes: int) -> bool:
    return (17 * 60 + 30) <= minutes < (18 * 60 + 30)


def _promote_one(attio: AttioClient, company_id: str, entry: dict) -> None:
    """Add the company to Deal Pipeline. Only non-list-mutating call here."""
    entry_values = _pipeline_entry_values(entry, attio)
    attio.add_record_to_list(
        list_id=DEAL_PIPELINE_LIST_ID,
        parent_record_id=company_id,
        parent_object=PARENT_OBJECT,
        entry_values=entry_values,
        allow_duplicates=False,
    )


def _pipeline_entry_values(entry: dict, attio: AttioClient) -> dict:
    """Build entry_values for the new Deal Pipeline entry.

    Set on every promotion:
      stage       = PIPELINE_DEFAULT_STAGE ("Outreach")
      review_date = today's date (Europe/Copenhagen)
    Carried over from the Inbound entry when present:
      sourcer    (actor-reference, same shape on both lists)
      deal_lead  (actor-reference, same shape on both lists)
      sourcing_channel (parsed from the Inbound source text suffix
        "(<channel>)", validated against PIPELINE_SOURCING_CHANNELS)
      source     (record-reference on Pipeline) — looked up from the
        Inbound source text body via fuzzy match against People then
        Companies. Skipped if no high-confidence match.
      upcoming_round / upcoming_round_size_eum — kept for completeness,
        not currently populated on Inbound.
    """
    today_local = datetime.now(ZoneInfo("Europe/Copenhagen")).date().isoformat()
    values: dict = {
        "stage": PIPELINE_DEFAULT_STAGE,
        "review_date": today_local,
    }

    inbound_values = entry.get("entry_values") or {}

    # Sourcer (actor-reference) — copy as-is.
    sourcer_refs = _extract_actor_refs(inbound_values.get("sourcer"))
    if sourcer_refs:
        values["sourcer"] = sourcer_refs

    # Deal Lead (actor-reference) — copy as-is, preserves order.
    lead_refs = _extract_actor_refs(inbound_values.get("deal_lead"))
    if lead_refs:
        values["deal_lead"] = lead_refs

    # Source text -> sourcing_channel + source record references.
    source_text = _extract_text(inbound_values.get("source"))
    body, channel = _parse_source_text(source_text)
    if channel and channel in PIPELINE_SOURCING_CHANNELS:
        values["sourcing_channel"] = [channel]
    if body:
        source_refs = _match_source_records(attio, body)
        if source_refs:
            values["source"] = source_refs

    # upcoming_round — only include valid in-scope values
    upcoming_round = _extract_select(inbound_values.get("upcoming_round"))
    if upcoming_round and upcoming_round in IN_SCOPE_STAGES:
        values["upcoming_round"] = [upcoming_round]

    # upcoming_round_size_eum
    size = _extract_number(inbound_values.get("upcoming_round_size_eum"))
    if size is not None:
        values["upcoming_round_size_eum"] = size

    return values


_SOURCE_CHANNEL_SUFFIX_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_SOURCE_FROM_SPLIT_RE = re.compile(r"\s+from\s+", re.IGNORECASE)


def _parse_source_text(text: str | None) -> tuple[str | None, str | None]:
    """Split an Inbound source text into (body, channel).

    Examples:
      "Hillary from TestCo VC (VC)" -> ("Hillary from TestCo VC", "VC")
      "Tom Smith (Angel)"           -> ("Tom Smith", "Angel")
      "(VC)"                        -> (None, "VC")
      "Just a name"                 -> ("Just a name", None)
    """
    if not text:
        return None, None
    text = text.strip()
    m = _SOURCE_CHANNEL_SUFFIX_RE.match(text)
    if m:
        body = m.group(1).strip() or None
        channel = m.group(2).strip() or None
        return body, channel
    return text, None


def _match_source_records(
    attio: AttioClient, body: str
) -> list[dict]:
    """Look up Attio People / Companies that match the source body.

    Strategy:
      1. If the text has "X from Y", look up X in People and Y in Companies.
      2. Else, try to match the whole text against People (more common to be
         a person name) and then against Companies as a fallback.

    Returns a list of record references in Pipeline's write shape:
      [{"target_object": "people"|"companies", "target_record_id": "..."}]
    Empty list if nothing matched well enough.
    """
    refs: list[dict] = []
    parts = _SOURCE_FROM_SPLIT_RE.split(body, maxsplit=1)
    if len(parts) == 2:
        person_part = parts[0].strip()
        firm_part = parts[1].strip()
        person = _lookup_person_by_name(attio, person_part)
        if person:
            refs.append({"target_object": "people", "target_record_id": person})
        company = _lookup_company_by_name(attio, firm_part)
        if company:
            refs.append({"target_object": "companies", "target_record_id": company})
        return refs

    # Single segment — try person first, then company.
    person = _lookup_person_by_name(attio, body)
    if person:
        refs.append({"target_object": "people", "target_record_id": person})
        return refs
    company = _lookup_company_by_name(attio, body)
    if company:
        refs.append({"target_object": "companies", "target_record_id": company})
    return refs


def _lookup_person_by_name(attio: AttioClient, name: str) -> str | None:
    """Fuzzy-match a Person record by name; return record_id of best match."""
    name = name.strip()
    if len(name) < 2:
        return None
    token = _first_token(name)
    if not token:
        return None
    try:
        candidates = attio.find_people_by_name_contains(token, limit=20)
    except Exception:
        return None
    best_id = None
    best_score = 0
    for c in candidates:
        cand = AttioClient.person_name(c)
        if not cand:
            continue
        score = fuzz.ratio(name.lower(), cand.lower())
        if score >= NAME_FUZZY_THRESHOLD and score > best_score:
            best_id = (c.get("id") or {}).get("record_id")
            best_score = score
    return best_id


def _lookup_company_by_name(attio: AttioClient, name: str) -> str | None:
    """Fuzzy-match a Company record by name; return record_id of best match."""
    name = name.strip()
    if len(name) < 2:
        return None
    token = _first_token(name)
    if not token:
        return None
    try:
        candidates = attio.find_companies_by_name_contains(token, limit=50)
    except Exception:
        return None
    best_id = None
    best_score = 0
    for c in candidates:
        cand = AttioClient.company_name(c)
        if not cand:
            continue
        score = fuzz.ratio(name.lower(), cand.lower())
        if score >= NAME_FUZZY_THRESHOLD and score > best_score:
            best_id = (c.get("id") or {}).get("record_id")
            best_score = score
    return best_id


def _first_token(s: str) -> str | None:
    """Return the first alphanumeric token from a string (used for the
    Attio $contains prefilter)."""
    for raw in re.split(r"[\s,\.]+", s.strip()):
        token = re.sub(r"[^A-Za-z0-9]", "", raw)
        if token:
            return token
    return None


def _extract_text(v) -> str | None:
    """Extract a plain-text value from Attio's read shape."""
    if not v:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list) and v:
        return _extract_text(v[0])
    if isinstance(v, dict):
        return v.get("value") or None
    return None


def _extract_actor_refs(v) -> list[dict]:
    """Convert Attio actor-reference values from read shape to write shape.

    Attio responses can return actor-references in a couple of shapes
    depending on the surface:
      [{"referenced_actor_type": "...", "referenced_actor_id": "..."}, ...]
      [{"actor": {"type": "...", "id": "..."}}, ...]
    We re-emit them in the write-shape Attio expects when creating the
    Pipeline entry.
    """
    if not v:
        return []
    if not isinstance(v, list):
        v = [v]
    out: list[dict] = []
    for item in v:
        if not isinstance(item, dict):
            continue
        actor_id = item.get("referenced_actor_id")
        actor_type = item.get("referenced_actor_type")
        if not actor_id:
            inner = item.get("actor") or {}
            actor_type = inner.get("type")
            actor_id = inner.get("id")
        if actor_id:
            out.append(
                {
                    "referenced_actor_type": actor_type or "workspace-member",
                    "referenced_actor_id": actor_id,
                }
            )
    return out


def _mark_added(
    attio: AttioClient,
    entry_id: str,
    failed: list[tuple[str, str]],
    company_name: str,
) -> None:
    try:
        attio.update_list_entry(
            list_id=INBOUND_DEALS_LIST_ID,
            entry_id=entry_id,
            entry_values={"step": STEP_ADDED},
        )
    except AttioError as e:
        print(f"{company_name}: failed to set step=Added: {e}")
        failed.append((company_name, "could not flip step to Added"))


def _is_already_exists(err_msg: str) -> bool:
    lower = err_msg.lower()
    return (
        "already exists" in lower
        or "duplicate" in lower
        or "already in list" in lower
    )


def _resolve_company_name(
    attio: AttioClient, entry: dict, company_id: str | None
) -> str:
    # Try to read a name from the entry's parent record if inlined; else fall
    # back to a short form of the company id.
    parent = entry.get("parent_record") or entry.get("parent")
    if isinstance(parent, dict):
        name = AttioClient.company_name(parent)
        if name:
            return name
    if company_id:
        return f"company:{company_id[:8]}"
    return "unknown"


def _extract_select(v) -> str | None:
    if not v:
        return None
    if isinstance(v, list) and v:
        item = v[0]
        if isinstance(item, dict):
            opt = item.get("option") or {}
            return opt.get("title") or item.get("value")
    if isinstance(v, dict):
        opt = v.get("option") or {}
        return opt.get("title") or v.get("value")
    if isinstance(v, str):
        return v
    return None


def _extract_number(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list) and v:
        return _extract_number(v[0])
    if isinstance(v, dict):
        for k in ("value", "number_value"):
            if k in v and v[k] is not None:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    return None
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _post_summary(
    slack: SlackClient,
    promoted: list[str],
    failed: list[tuple[str, str]],
    open_breakdown: Counter | None,
) -> None:
    has_open = bool(open_breakdown)
    if not promoted and not failed and not has_open:
        print("Nothing to promote and no open deals — skipping post.")
        return

    lines: list[str] = []
    if promoted:
        lines.append(
            f"🗓️ Daily promotion: moved {len(promoted)} deal(s) to Deal Pipeline."
        )
        for name in promoted:
            lines.append(f"• {name}")
    else:
        lines.append("🗓️ Daily promotion: nothing new moved to Deal Pipeline.")

    if failed:
        lines.append("")
        lines.append(f"⚠️ {len(failed)} failed:")
        for name, why in failed:
            lines.append(f"• {name} — {why}")

    if has_open:
        lines.append("")
        lines.append("*Still waiting for review:*")
        # Sort: highest count first; unassigned bucket last regardless.
        items = sorted(
            open_breakdown.items(),
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

    try:
        slack.post_message("\n".join(lines))
    except Exception as e:
        print(f"Failed to post Slack summary: {e}")


def _short(s: str, n: int = 160) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# ---------------------------------------------------------------------
# Cleanup: archive Duplicate / Passed (<100 days) entries whose parent
# deal is no longer being actively triaged.
# ---------------------------------------------------------------------

# Pipeline statuses that signal "no longer being worked on".
PIPELINE_DONE_STATUSES = frozenset(
    {"To Pass", "Action Tracking", "Tracking", "Passed", "Lost"}
)
# Inbound steps that signal "we already passed".
INBOUND_DONE_STEPS = frozenset({"Not relevant", "Passed (<100 days)"})


def _archive_completed_duplicates(attio: AttioClient) -> None:
    """For every Inbound entry with Step ∈ {Duplicate, Passed (<100 days)},
    if the parent Company has any other Inbound or Pipeline entry in a
    'done' state, flip this entry's Step to Not relevant.

    Pipeline 'done' = status ∈ {To Pass, Action Tracking, Tracking,
    Passed, Lost}.
    Inbound 'done' = step ∈ {Not relevant, Passed (<100 days)} —
    ignoring the entry currently being checked.

    Logs counts to the Actions log; no Slack post.
    """
    candidates: list[dict] = []
    for step in (STEP_DUPLICATE, STEP_PASSED_RECENT):
        try:
            entries = attio.query_list_entries(
                INBOUND_DEALS_LIST_ID,
                filter_={"step": step},
                limit=200,
            )
        except Exception as e:
            print(f"[cleanup] failed to fetch step={step}: {e}")
            continue
        candidates.extend(entries)

    if not candidates:
        print("[cleanup] no Duplicate / Passed (<100 days) entries to check")
        return

    # Cache Pipeline-done check per company_id; the Inbound check has to
    # be redone per entry because we exclude the entry being checked.
    pipeline_done_cache: dict[str, bool] = {}
    archived = 0
    skipped_active = 0
    failed: list[str] = []

    for entry in candidates:
        entry_id = AttioClient.entry_id(entry)
        company_id = AttioClient.parent_record_id(entry)
        if not entry_id or not company_id:
            continue

        if not _company_done(
            attio, company_id, exclude_entry_id=entry_id, cache=pipeline_done_cache
        ):
            skipped_active += 1
            continue

        try:
            attio.update_list_entry(
                list_id=INBOUND_DEALS_LIST_ID,
                entry_id=entry_id,
                entry_values={"step": STEP_NOT_RELEVANT},
            )
            archived += 1
        except Exception as e:
            print(f"[cleanup] failed to archive entry {entry_id}: {e}")
            failed.append(entry_id)

    print(
        f"[cleanup] checked {len(candidates)} entries, "
        f"archived {archived}, skipped {skipped_active} (parent still active), "
        f"failed {len(failed)}"
    )


def _company_done(
    attio: AttioClient,
    company_id: str,
    *,
    exclude_entry_id: str | None,
    cache: dict[str, bool],
) -> bool:
    """True if this Company has any Pipeline entry in a done status, OR
    any other Inbound entry (excluding exclude_entry_id) in a done step."""
    # Pipeline check is cacheable (doesn't depend on the entry being checked).
    if company_id in cache:
        if cache[company_id]:
            return True
    else:
        try:
            pipeline_entries = attio.find_list_entries_for_company(
                DEAL_PIPELINE_LIST_ID, company_id, limit=10
            )
            done = any(
                extract_pipeline_stage(e) in PIPELINE_DONE_STATUSES
                for e in pipeline_entries
            )
        except Exception as e:
            print(f"[cleanup] pipeline check failed for {company_id}: {e}")
            done = False
        cache[company_id] = done
        if done:
            return True

    # Inbound check — must exclude the entry being evaluated, otherwise
    # a Passed (<100 days) entry would always trigger archive of itself.
    try:
        inbound_entries = attio.find_list_entries_for_company(
            INBOUND_DEALS_LIST_ID, company_id, limit=20
        )
        for e in inbound_entries:
            if AttioClient.entry_id(e) == exclude_entry_id:
                continue
            if extract_inbound_step(e) in INBOUND_DONE_STEPS:
                return True
    except Exception as e:
        print(f"[cleanup] inbound check failed for {company_id}: {e}")

    return False


if __name__ == "__main__":
    sys.exit(main())
