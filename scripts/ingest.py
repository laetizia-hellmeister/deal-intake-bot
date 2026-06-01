"""Ingest workflow entry point.

Polls the Slack deal-intake channel, processes any message that has no
bot reaction yet, and either stages each deal in the message in Attio
Inbound Deals or skips it (with a reaction marking the overall outcome).

A single Slack message may contain multiple deals (e.g. a bulleted list);
each is processed independently. The threaded reply summarises every
deal as a bulleted line; the reaction reflects the best outcome:
  - ✅ if at least one deal was added
  - 🔁 if all detected deals were duplicates
  - ⏭️ if all detected deals were out of scope (or any mix of dupes/skips
        with at least one skip)
  - 🤷 if no deals were detected
  - ⚠️ on any unhandled error
"""

from __future__ import annotations

import re
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic

from attio_client import AttioClient, AttioError
from config import (
    ANTHROPIC_API_KEY,
    INBOUND_DEALS_LIST_ID,
    INGEST_LOOKBACK_SECONDS,
    INGEST_MESSAGE_LIMIT,
    NAME_FUZZY_THRESHOLD,
    OUT_OF_SCOPE_STAGES,
    PARENT_OBJECT,
    REACTION_ADDED,
    REACTION_DUPLICATE,
    REACTION_ERROR,
    REACTION_NOT_DEAL,
    REACTION_PASSED_RECENT,
    REACTION_RESURFACE,
    REACTION_SKIPPED,
    SLACK_USER_TO_ATTIO_MEMBER,
    STEP_ADD_TO_PIPELINE,
    STEP_DUPLICATE,
    STEP_NEW,
    STEP_NEW_RESURFACING,
    STEP_PASSED_RECENT,
)
from dedupe import find_duplicate, location_label, _first_significant_token
from extractor import extract_deals
from rapidfuzz import fuzz
from slack_client import SlackClient


def main() -> int:
    slack = SlackClient()
    attio = AttioClient()
    anthro = Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        messages = slack.fetch_recent_messages(
            lookback_seconds=INGEST_LOOKBACK_SECONDS,
            limit=INGEST_MESSAGE_LIMIT,
        )
    except Exception as e:
        print(f"Failed to fetch Slack messages: {e}")
        attio.close()
        return 1

    # Oldest first, so reactions appear in chronological order
    messages.sort(key=lambda m: float(m.get("ts", "0")))

    # Filter once so we know if there's actually any work to do.
    actionable = [
        m
        for m in messages
        if not SlackClient.is_from_bot(m)
        and not SlackClient.is_thread_reply(m)
        and not SlackClient.has_processed_reaction(m)
    ]
    skipped_already = sum(
        1
        for m in messages
        if not SlackClient.is_from_bot(m)
        and not SlackClient.is_thread_reply(m)
        and SlackClient.has_processed_reaction(m)
    )

    # Pre-fetch list-entry indices ONCE per ingest run if we're going to
    # do any dedupe work. Each subsequent _process_one_deal call gets
    # O(1) lookups instead of paginating Inbound + Pipeline per match.
    # Skipped entirely on quiet ticks so the typical no-op cron run stays
    # cheap.
    inbound_index: dict[str, list[dict]] | None = None
    pipeline_index: dict[str, list[dict]] | None = None
    if actionable:
        try:
            inbound_index = attio.build_company_index(INBOUND_DEALS_LIST_ID)
        except Exception as e:
            print(f"Failed to pre-fetch Inbound index: {e}")
        try:
            pipeline_index = attio.build_company_index(DEAL_PIPELINE_LIST_ID)
        except Exception as e:
            print(f"Failed to pre-fetch Pipeline index: {e}")

    handled = 0
    for msg in actionable:
        _process_message(
            slack,
            attio,
            anthro,
            msg,
            inbound_index=inbound_index,
            pipeline_index=pipeline_index,
        )
        handled += 1

    print(
        f"Ingest complete. Processed {handled} message(s), "
        f"skipped {skipped_already} already-reacted, of {len(messages)} fetched."
    )
    attio.close()
    return 0


def _process_message(
    slack: SlackClient,
    attio: AttioClient,
    anthro: Anthropic,
    msg: dict,
    *,
    inbound_index: dict[str, list[dict]] | None = None,
    pipeline_index: dict[str, list[dict]] | None = None,
) -> None:
    ts = msg.get("ts")
    text = msg.get("text") or ""
    if not ts:
        return

    try:
        # Pitchdecks / memos attached to the message get pulled in too.
        # Currently PDF is the only natively-supported document type;
        # other mimes are silently skipped by the extractor.
        documents = _download_message_documents(slack, msg)
        had_attachment = bool(documents)

        deals = extract_deals(text, client=anthro, documents=documents)

        # Drop items the LLM explicitly marked as not-a-deal (defensive —
        # the prompt asks for an empty array on chatter, but old shapes
        # may still emit is_deal=false items).
        deals = [d for d in deals if d.get("is_deal")]

        # No deals at all -> silent 🤷
        if not deals:
            slack.add_reaction(ts, REACTION_NOT_DEAL)
            return

        permalink = slack.permalink(ts) or ""
        slack_user = msg.get("user")
        poster_member = SLACK_USER_TO_ATTIO_MEMBER.get(slack_user)
        fallback_source = _fallback_source(slack, msg)

        # Sourcer = always the Slack poster (single value).
        sourcer_member = poster_member

        # Message-level @-mentions — the default Deal Lead pool. Each
        # deal can override this by setting its own `assigned_user_ids`
        # (resolved per-deal inside _process_one_deal).
        message_mention_members = [
            SLACK_USER_TO_ATTIO_MEMBER[uid]
            for uid in _extract_slack_mentions(text)
            if uid in SLACK_USER_TO_ATTIO_MEMBER
        ]

        # Process each deal independently; collect a per-deal outcome line.
        outcomes: list[dict[str, Any]] = []
        for deal in deals:
            outcomes.append(
                _process_one_deal(
                    attio=attio,
                    deal=deal,
                    permalink=permalink,
                    sourcer_member=sourcer_member,
                    poster_member=poster_member,
                    message_mention_members=message_mention_members,
                    fallback_source=fallback_source,
                    inbound_index=inbound_index,
                    pipeline_index=pipeline_index,
                    had_attachment=had_attachment,
                )
            )

        # Build the threaded reply (one bullet per deal) + pick the
        # message-level reaction.
        reply = "\n".join(o["line"] for o in outcomes)
        slack.post_thread_reply(ts, reply)
        slack.add_reaction(ts, _aggregate_reaction(outcomes))

    except Exception as e:
        # Always react ⚠️ on error so we don't retry forever.
        short = _short_error(e)
        print(f"Error processing ts={ts}: {short}")
        traceback.print_exc()
        try:
            slack.post_thread_reply(
                ts,
                f"⚠️ Couldn't process this message: {short}\n"
                f"Marked as reviewed. Remove the ⚠️ reaction to retry.",
            )
        except Exception:
            pass
        try:
            slack.add_reaction(ts, REACTION_ERROR)
        except Exception:
            pass


_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def _extract_slack_mentions(text: str) -> list[str]:
    """Return Slack user IDs mentioned in a message, in order, deduplicated.

    Slack renders @-mentions in message text as `<@U123ABC>` or
    `<@U123ABC|name>`. User IDs start with U (humans) or W (enterprise
    grid). We don't try to fetch display names — just IDs.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for uid in _MENTION_RE.findall(text):
        if uid not in seen:
            out.append(uid)
            seen.add(uid)
    return out


def _process_one_deal(
    *,
    attio: AttioClient,
    deal: dict[str, Any],
    permalink: str,
    sourcer_member: str | None,
    poster_member: str | None,
    message_mention_members: list[str],
    fallback_source: str | None,
    inbound_index: dict[str, list[dict]] | None = None,
    pipeline_index: dict[str, list[dict]] | None = None,
    had_attachment: bool = False,
) -> dict[str, Any]:
    """Process a single deal extracted from a Slack message.

    Returns a dict with:
      outcome: "added" | "duplicate" | "out_of_scope" | "error"
      line: the bullet-list entry for the threaded reply
    """
    # Apply stealth-name heuristic: if no company name but founders exist,
    # synthesise "Stealth (Founder1, Founder2)" so we have something to
    # show + store in Attio. Dedupe-by-founder-LinkedIn (when wired up)
    # is what actually prevents duplicate stealth records.
    deal = _apply_stealth_name(deal)

    # Resolve this deal's Deal Lead pool. If the LLM extracted per-deal
    # assigned_user_ids (e.g. "@pranav take Acme, @shrey take Beta"),
    # they override the message-level mentions; otherwise fall back to
    # whoever was @-mentioned at the message level. Poster is always
    # appended last so they're a fallback Lead too.
    per_deal_ids = deal.get("assigned_user_ids") or []
    if per_deal_ids:
        deal_mention_members = [
            SLACK_USER_TO_ATTIO_MEMBER[uid]
            for uid in per_deal_ids
            if uid in SLACK_USER_TO_ATTIO_MEMBER
        ]
    else:
        deal_mention_members = list(message_mention_members)
    lead_members: list[str] = []
    for m in deal_mention_members + ([poster_member] if poster_member else []):
        if m and m not in lead_members:
            lead_members.append(m)

    company_name = deal.get("company_name") or "unknown company"
    stage = deal.get("stage")
    # Treat "Unknown" and missing as the same — both pass the scope check.
    stage_for_scope = stage if stage and stage != "Unknown" else None

    # Founder LinkedIn URLs — written to the Inbound Deals `founder_linkedins`
    # multi-text attribute so they're visible at a glance for triage.
    founder_linkedins = [
        f["linkedin"]
        for f in (deal.get("founders") or [])
        if f.get("linkedin")
    ]

    try:
        # Geo filter: a US flag emoji explicitly preceding the deal in
        # Slack is a hard skip — we're Europe-focused. The LLM sets
        # `geo_skip=true` only on lines literally prefixed with 🇺🇸
        # or :us:, never inferred from name/domain.
        if deal.get("geo_skip"):
            line = f"🇺🇸 {company_name} — skipped (US, out of geo)"
            return {"outcome": "out_of_geo", "line": line}

        # Out of scope only when the stage is *explicitly* Series A or later.
        # Missing / unknown stages are added with stage left blank.
        if stage_for_scope in OUT_OF_SCOPE_STAGES:
            line = (
                f"⏭️ {company_name} — out of scope "
                f"(stage: {stage_for_scope})"
            )
            return {"outcome": "out_of_scope", "line": line}

        # Dedupe. Granular flags from DedupeMatch determine the outcome:
        #   recent_inbound                  -> Duplicate
        #   pipeline_has_active             -> Duplicate
        #   pipeline_has_recent_terminal    -> Duplicate (will become its
        #                                       own Step "passed < 60 days"
        #                                       once that option is added
        #                                       to Inbound.Step in Attio)
        #   match exists but none of above  -> Resurface (Step=New, 🦖)
        #   no match                        -> truly new (Step=New, ✅)
        match = find_duplicate(
            attio,
            deal,
            inbound_index=inbound_index,
            pipeline_index=pipeline_index,
        )
        days_since_first_seen = _days_since_first_seen(match)
        duplicate_kind = _classify_duplicate(match)
        if duplicate_kind:
            attio_url = AttioClient.company_web_url(match.company_id or "")
            existing_loc = location_label(match)
            source = _format_source(deal, fallback_source)
            dup_description = _build_duplicate_description(
                deal, permalink, attio_url, existing_loc
            )
            # passed_recent gets its own Step + emoji + outcome so that
            # Laetizia can filter the Inbound Deals view to hide deals
            # that were recently passed in Pipeline. The other two
            # duplicate kinds use the existing Duplicate step.
            if duplicate_kind == "passed_recent":
                step = STEP_PASSED_RECENT
                emoji = "👎"
                outcome = "passed_recent"
                kind_label = "recently passed in Deal Pipeline"
            else:
                step = STEP_DUPLICATE
                emoji = "🔁"
                outcome = "duplicate"
                kind_label = (
                    "already in Inbound Deals"
                    if duplicate_kind == "recent_inbound"
                    else "active in Deal Pipeline"
                    if duplicate_kind == "pipeline_active"
                    else f"already in {existing_loc}"
                )
            entry_values = _build_inbound_entry_values(
                step=step,
                source=source,
                description=dup_description,
                sourcer_member=sourcer_member,
                lead_members=lead_members,
                founder_linkedins=founder_linkedins,
                days_since_first_seen=days_since_first_seen,
            )
            attio.add_record_to_list(
                list_id=INBOUND_DEALS_LIST_ID,
                parent_record_id=match.company_id,
                parent_object=PARENT_OBJECT,
                entry_values=entry_values,
                allow_duplicates=True,
            )
            line = f"{emoji} {company_name} — {kind_label} ({attio_url})"
            return {"outcome": outcome, "line": line}

        # New deal path. Two sub-cases:
        #   - No match at all: upsert/create the Company.
        #   - Stale match (Company exists but no recent Inbound, no
        #     active Pipeline, no recent terminal Pipeline): treat as
        #     a resurface — Step=New but with a 🦖 line so it's
        #     visually distinct from a truly new deal.
        is_resurface = bool(match)
        if match:
            company_id = match.company_id
            if not company_id:
                raise RuntimeError("Stale match had no company_id")
        else:
            company_record = _upsert_company(attio, deal)
            company_id = AttioClient.record_id(company_record)
            if not company_id:
                raise RuntimeError("Attio did not return a company record id")

        source = _format_source(deal, fallback_source)
        description = _build_inbound_description(
            deal, permalink, had_attachment=had_attachment
        )

        # `direct_to_pipeline=true` from the extractor means the user
        # explicitly said "add to pipeline" / "skip triage" / etc. The
        # Inbound entry's Step jumps straight to "Add to pipeline"
        # so the next promote run picks it up — no manual triage needed.
        direct = bool(deal.get("direct_to_pipeline"))
        if direct:
            step_value = STEP_ADD_TO_PIPELINE
        elif is_resurface:
            step_value = STEP_NEW_RESURFACING
        else:
            step_value = STEP_NEW

        entry_values = _build_inbound_entry_values(
            step=step_value,
            source=source,
            description=description,
            sourcer_member=sourcer_member,
            lead_members=lead_members,
            founder_linkedins=founder_linkedins,
            days_since_first_seen=days_since_first_seen,
        )

        attio.add_record_to_list(
            list_id=INBOUND_DEALS_LIST_ID,
            parent_record_id=company_id,
            parent_object=PARENT_OBJECT,
            entry_values=entry_values,
            allow_duplicates=False,
        )

        round_size = deal.get("round_size_eur_m")
        round_str = f"€{round_size}M" if round_size else "€?"
        sector = deal.get("sector") or "?"
        stage_label = stage_for_scope or "stage ?"
        attio_url = AttioClient.company_web_url(company_id)
        deck_suffix = " 📎" if had_attachment else ""
        if direct:
            tag = "(resurfacing, direct)" if is_resurface else "(direct)"
            line = (
                f"🚀 {company_name} {tag} · {stage_label} · "
                f"{round_str} · {sector}{deck_suffix} ({attio_url})"
            )
            return {"outcome": "added", "line": line}
        if is_resurface:
            line = (
                f"🦖 {company_name} (resurfacing) · {stage_label} · "
                f"{round_str} · {sector}{deck_suffix} ({attio_url})"
            )
            return {"outcome": "resurfacing", "line": line}
        line = (
            f"✅ {company_name} · {stage_label} · {round_str} · {sector}"
            f"{deck_suffix} ({attio_url})"
        )
        return {"outcome": "added", "line": line}

    except Exception as e:
        # Per-deal error: don't blow up the whole message, just record an
        # error line and continue.
        short = _short_error(e)
        print(f"Error processing deal '{company_name}': {short}")
        traceback.print_exc()
        line = f"⚠️ {company_name} — error: {short}"
        return {"outcome": "error", "line": line}


def _aggregate_reaction(outcomes: list[dict[str, Any]]) -> str:
    """Pick the single message-level reaction from per-deal outcomes.

    Priority: ⚠️ > ✅ > 🦖 > 🔁 > 👎 > ⏭️ > 🤷.
    A mixed message (e.g. one new deal + one resurface) gets ✅ — the
    truly-new outcome dominates. 🔁 wins over 👎 when both kinds of
    duplicate are present, so 👎 only appears when the *whole* message
    is recent-passes.
    """
    types = {o["outcome"] for o in outcomes}
    if "error" in types:
        return REACTION_ERROR
    if "added" in types:
        return REACTION_ADDED
    if "resurfacing" in types:
        return REACTION_RESURFACE
    if "duplicate" in types:
        return REACTION_DUPLICATE
    if "passed_recent" in types:
        return REACTION_PASSED_RECENT
    if "out_of_scope" in types or "out_of_geo" in types:
        return REACTION_SKIPPED
    return REACTION_NOT_DEAL


def _classify_duplicate(match) -> str | None:
    """Map a DedupeMatch to a duplicate kind, or None if not a duplicate.

    Order matters — checks in order of strongest signal:
      1. recent_inbound       -> the deal was just shared in Inbound
      2. pipeline_has_active  -> currently being worked in Pipeline
      3. pipeline_has_recent_terminal -> recently Passed/Lost in Pipeline
      4. (none)               -> stale or no match; not a duplicate
    """
    if not match:
        return None
    if match.recent_inbound:
        return "recent_inbound"
    if match.pipeline_has_active:
        return "pipeline_active"
    if match.pipeline_has_recent_terminal:
        return "passed_recent"
    return None


def _apply_stealth_name(deal: dict[str, Any]) -> dict[str, Any]:
    """If a deal has no company name but does have founders, synthesise
    a 'Stealth (Founder Names)' name so we have something to display
    and store. Returns a (possibly modified) copy."""
    if deal.get("company_name"):
        return deal
    founders = deal.get("founders") or []
    names = [f["name"] for f in founders if f.get("name")]
    if not names:
        return deal
    deal = dict(deal)
    deal["company_name"] = f"Stealth ({', '.join(names)})"
    return deal


def _upsert_company(attio: AttioClient, deal: dict[str, Any]) -> dict:
    """Create or upsert a company. Prefer upsert-by-domain; fall back to create.

    On *creation* (no domain → fresh company), also look up matching
    People records by founder LinkedIn / name and include them in the
    Company's `team` attribute. We don't touch `team` on assert_company
    calls because the company already exists in Attio and may have
    pre-existing team links we don't want to overwrite.
    """
    values: dict[str, Any] = {}

    name = deal.get("company_name")
    if name:
        values["name"] = name

    # Drop a domain that's actually linkedin.com — happens when the LLM
    # extracts a founder's LinkedIn profile URL as the company URL. The
    # company's domain isn't linkedin.com.
    domain = deal.get("domain")
    if domain and domain.lower() not in ("linkedin.com", "www.linkedin.com"):
        values["domains"] = [domain]
    else:
        domain = None  # treat as no domain for the upsert/create branch

    description = deal.get("description")
    if description:
        values["description"] = description

    # Only set the company's `linkedin` field if it's actually a Company
    # LinkedIn page. Personal profile URLs (linkedin.com/in/...) belong on
    # the founder's People record (handled below via _resolve_existing_people),
    # not on the Company. Attio's Companies.linkedin attribute rejects
    # /in/ URLs with a 400 validation error.
    linkedin = deal.get("linkedin_url")
    if linkedin and not _is_personal_linkedin_url(linkedin):
        values["linkedin"] = linkedin

    if domain:
        # Existing-or-new path; don't touch team to avoid clobber.
        return attio.assert_company(values, matching="domains")

    # Fresh-company path — also link any matched existing People.
    person_ids = _resolve_existing_people(attio, deal.get("founders") or [])
    if person_ids:
        values["team"] = [
            {"target_object": "people", "target_record_id": pid}
            for pid in person_ids
        ]
    return attio.create_company(values)


def _is_personal_linkedin_url(url: str | None) -> bool:
    """True if the URL is a personal LinkedIn profile (vs. a Company page)."""
    if not url:
        return False
    lower = url.lower()
    return "/in/" in lower or "/pub/" in lower


def _resolve_existing_people(
    attio: AttioClient, founders: list[dict[str, Any]]
) -> list[str]:
    """For each founder, find an existing Attio Person record (by LinkedIn,
    then by fuzzy name). Returns a deduplicated list of matched record IDs.
    Does NOT create new People — only links to existing ones.
    """
    matched: list[str] = []
    seen: set[str] = set()
    for founder in founders:
        person = _lookup_person(attio, founder)
        if not person:
            continue
        pid = (person.get("id") or {}).get("record_id")
        if pid and pid not in seen:
            matched.append(pid)
            seen.add(pid)
    return matched


def _lookup_person(
    attio: AttioClient, founder: dict[str, Any]
) -> dict | None:
    # 1) Exact match on LinkedIn URL.
    linkedin = founder.get("linkedin")
    if linkedin:
        try:
            people = attio.find_people_by_linkedin(linkedin)
            if people:
                return people[0]
        except Exception as e:
            print(f"[people-lookup] linkedin search failed for {linkedin}: {e}")

    # 2) Fuzzy name match.
    name = founder.get("name")
    if not name:
        return None
    token = _first_significant_token(name)
    if not token:
        return None
    try:
        candidates = attio.find_people_by_name_contains(token, limit=20)
    except Exception as e:
        print(f"[people-lookup] name search failed for {name}: {e}")
        return None
    best = None
    best_score = 0
    for c in candidates:
        cand = AttioClient.person_name(c)
        if not cand:
            continue
        score = fuzz.ratio(name.lower(), cand.lower())
        if score >= NAME_FUZZY_THRESHOLD and score > best_score:
            best = c
            best_score = score
    return best


def _format_source(deal: dict[str, Any], fallback: str | None) -> str | None:
    """Build the Inbound Deals `source` text.

    Combines the LLM's source string (or the Slack-poster fallback) with
    the detected sourcing_channel suffix in parens, e.g.:
        "Hillary from TestCo VC (VC)"
        "shared by Tom Smith (Angel)"
    Promote.py parses the trailing parens back out at promotion time
    to set Pipeline's sourcing_channel select attribute.
    """
    body = deal.get("source") or fallback
    channel = deal.get("sourcing_channel")
    if body and channel:
        return f"{body} ({channel})"
    if body:
        return body
    if channel:
        return f"({channel})"
    return None


def _days_since_first_seen(match) -> int:
    """Days between now and the earliest Inbound/Pipeline entry for the
    matched Company. Truly-new deals (no match) get 0."""
    if not match or not getattr(match, "first_seen_at", None):
        return 0
    delta = datetime.now(timezone.utc) - match.first_seen_at
    return max(0, delta.days)


def _build_inbound_entry_values(
    *,
    step: str,
    source: str | None,
    description: str | None,
    sourcer_member: str | None,
    lead_members: list[str],
    founder_linkedins: list[str] | None = None,
    days_since_first_seen: int = 0,
) -> dict[str, Any]:
    """Build the entry_values payload for an Inbound Deals entry, dropping
    any keys whose value is None/empty. Attio rejects explicit nulls on
    text attributes with a 400 validation_type error.

    sourcer_member: single Attio workspace_member_id of the Slack poster
                    (always — they sourced the deal into the channel).
    lead_members:   list of Attio workspace_member_ids — any @-mentioned
                    colleagues first, then the poster. Empty if nobody
                    is mappable.
    founder_linkedins: list of founder LinkedIn URLs extracted from the
                    message. Stored as a multi-value text attribute on
                    Inbound Deals so they're visible at a glance for
                    triage. Skipped if empty.
    """
    values: dict[str, Any] = {"step": step}
    if source:
        values["source"] = source
    if description:
        values["description"] = description
    if sourcer_member:
        values["sourcer"] = [
            {
                "referenced_actor_type": "workspace-member",
                "referenced_actor_id": sourcer_member,
            }
        ]
    if lead_members:
        values["deal_lead"] = [
            {
                "referenced_actor_type": "workspace-member",
                "referenced_actor_id": m,
            }
            for m in lead_members
        ]
    if founder_linkedins:
        # Inbound Deals' `founder_linkedin` is a single-value text
        # attribute, so multiple URLs are joined with newlines. If the
        # attribute is later switched to multi-value in Attio, this
        # still renders one URL per line cleanly; we can switch to a
        # list payload at that point.
        values["founder_linkedin"] = "\n".join(founder_linkedins)
    # Always set days_since_first_seen — int, default 0 for truly new.
    values["days_since_first_seen"] = days_since_first_seen
    return values


def _build_duplicate_description(
    deal: dict[str, Any],
    permalink: str,
    existing_attio_url: str,
    existing_loc: str,
) -> str:
    """Description for an Inbound entry created on a duplicate match."""
    bits: list[str] = [
        f"Duplicate share — already in: {existing_loc}",
        f"Existing: {existing_attio_url}",
    ]
    source = deal.get("source")
    if source:
        bits.append(f"Resharer: {source}")
    sector = deal.get("sector")
    if sector:
        bits.append(f"Sector: {sector}")
    stage = deal.get("stage")
    round_size = deal.get("round_size_eur_m")
    if stage and stage != "Unknown":
        round_str = f" (€{round_size}M)" if round_size else ""
        bits.append(f"Round: {stage}{round_str}")
    elif round_size:
        bits.append(f"Round: €{round_size}M")
    if permalink:
        bits.append(f"Slack: {permalink}")
    extra = deal.get("description")
    if extra:
        return f"{extra}\n\n" + "\n".join(bits)
    return "\n".join(bits)


def _build_inbound_description(
    deal: dict[str, Any], permalink: str, had_attachment: bool = False
) -> str:
    founders = deal.get("founders") or []
    founder_names = ", ".join(f["name"] for f in founders if f.get("name"))
    founder_linkedins = [
        f["linkedin"] for f in founders if f.get("linkedin")
    ]

    sector = deal.get("sector")
    stage = deal.get("stage")
    round_size = deal.get("round_size_eur_m")

    bits: list[str] = []
    if founder_names:
        bits.append(f"Founders: {founder_names}")
    if founder_linkedins:
        bits.append("Founder LinkedIns: " + ", ".join(founder_linkedins))
    if sector:
        bits.append(f"Sector: {sector}")
    if stage and stage != "Unknown":
        round_str = f" (€{round_size}M)" if round_size else ""
        bits.append(f"Round: {stage}{round_str}")
    elif round_size:
        bits.append(f"Round: €{round_size}M")
    if had_attachment:
        bits.append("📎 Pitchdeck / file attached in Slack")
    if permalink:
        bits.append(f"Slack: {permalink}")

    base = "\n".join(bits)
    extra = deal.get("description")
    if extra:
        base = f"{extra}\n\n{base}" if base else extra
    return base


def _download_message_documents(
    slack: SlackClient, msg: dict
) -> list[tuple[str, bytes]]:
    """Fetch any file attachments on the Slack message that we can pass
    to Claude as document blocks. Returns a list of (mime, bytes) tuples.

    Currently filters to PDF only — Claude handles those natively. Other
    types (PPTX, DOCX, images) are skipped with a log line; we can add
    handling later if needed. Requires the bot to have the `files:read`
    OAuth scope.
    """
    files = msg.get("files") or []
    if not files:
        return []
    out: list[tuple[str, bytes]] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        mime = (f.get("mimetype") or "").lower()
        url = f.get("url_private_download") or f.get("url_private")
        name = f.get("name", "?")
        if mime != "application/pdf":
            print(
                f"[ingest] skipping attachment {name!r} — mime={mime!r} "
                "not supported (only PDF for now)"
            )
            continue
        if not url:
            continue
        data = slack.download_file(url)
        if not data:
            print(f"[ingest] failed to download attachment {name!r}")
            continue
        # Anthropic's per-document size cap is around 32 MB; we'll let
        # the API reject anything bigger rather than guess thresholds.
        print(f"[ingest] attached PDF {name!r} ({len(data)} bytes)")
        out.append((mime, data))
    return out


def _fallback_source(slack: SlackClient, msg: dict) -> str | None:
    user_id = msg.get("user")
    if not user_id:
        return None
    name = slack.user_display_name(user_id)
    return name


def _short_error(e: Exception) -> str:
    s = str(e) or e.__class__.__name__
    return s if len(s) <= 200 else s[:197] + "..."


if __name__ == "__main__":
    sys.exit(main())
