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
    REACTION_SKIPPED,
    SLACK_USER_TO_ATTIO_MEMBER,
    STEP_DUPLICATE,
    STEP_NEW,
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

    handled = 0
    skipped = 0
    for msg in messages:
        if SlackClient.is_from_bot(msg):
            continue
        if SlackClient.is_thread_reply(msg):
            continue
        if SlackClient.has_processed_reaction(msg):
            skipped += 1
            continue
        _process_message(slack, attio, anthro, msg)
        handled += 1

    print(
        f"Ingest complete. Processed {handled} message(s), "
        f"skipped {skipped} already-reacted, of {len(messages)} fetched."
    )
    attio.close()
    return 0


def _process_message(
    slack: SlackClient,
    attio: AttioClient,
    anthro: Anthropic,
    msg: dict,
) -> None:
    ts = msg.get("ts")
    text = msg.get("text") or ""
    if not ts:
        return

    try:
        deals = extract_deals(text, client=anthro)

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

        # Deal Lead = any @-mentioned colleague(s) first, then the poster.
        # Deduplicated, preserving order, using the same Slack→Attio map.
        mentioned_members = [
            SLACK_USER_TO_ATTIO_MEMBER[uid]
            for uid in _extract_slack_mentions(text)
            if uid in SLACK_USER_TO_ATTIO_MEMBER
        ]
        lead_members: list[str] = []
        for m in mentioned_members + ([poster_member] if poster_member else []):
            if m and m not in lead_members:
                lead_members.append(m)

        # Process each deal independently; collect a per-deal outcome line.
        outcomes: list[dict[str, Any]] = []
        for deal in deals:
            outcomes.append(
                _process_one_deal(
                    attio=attio,
                    deal=deal,
                    permalink=permalink,
                    sourcer_member=sourcer_member,
                    lead_members=lead_members,
                    fallback_source=fallback_source,
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
    lead_members: list[str],
    fallback_source: str | None,
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

    company_name = deal.get("company_name") or "unknown company"
    stage = deal.get("stage")
    # Treat "Unknown" and missing as the same — both pass the scope check.
    stage_for_scope = stage if stage and stage != "Unknown" else None

    try:
        # Out of scope only when the stage is *explicitly* Series A or later.
        # Missing / unknown stages are added with stage left blank.
        if stage_for_scope in OUT_OF_SCOPE_STAGES:
            line = (
                f"⏭️ {company_name} — out of scope "
                f"(stage: {stage_for_scope})"
            )
            return {"outcome": "out_of_scope", "line": line}

        # Dedupe — if the company already exists, still record this share
        # in Inbound Deals as a Duplicate entry so we can count repeat
        # shares.
        match = find_duplicate(attio, deal)
        if match:
            attio_url = AttioClient.company_web_url(match.company_id or "")
            existing_loc = location_label(match)
            source = _format_source(deal, fallback_source)
            dup_description = _build_duplicate_description(
                deal, permalink, attio_url, existing_loc
            )
            entry_values = _build_inbound_entry_values(
                step=STEP_DUPLICATE,
                source=source,
                description=dup_description,
                sourcer_member=sourcer_member,
                lead_members=lead_members,
            )
            attio.add_record_to_list(
                list_id=INBOUND_DEALS_LIST_ID,
                parent_record_id=match.company_id,
                parent_object=PARENT_OBJECT,
                entry_values=entry_values,
                allow_duplicates=True,
            )
            line = (
                f"🔁 {company_name} — already in {existing_loc}; "
                f"logged as Duplicate ({attio_url})"
            )
            return {"outcome": "duplicate", "line": line}

        # New -> upsert company + add to Inbound Deals
        company_record = _upsert_company(attio, deal)
        company_id = AttioClient.record_id(company_record)
        if not company_id:
            raise RuntimeError("Attio did not return a company record id")

        source = _format_source(deal, fallback_source)
        description = _build_inbound_description(deal, permalink)

        entry_values = _build_inbound_entry_values(
            step=STEP_NEW,
            source=source,
            description=description,
            sourcer_member=sourcer_member,
            lead_members=lead_members,
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
        line = (
            f"✅ {company_name} · {stage_label} · {round_str} · {sector} "
            f"({attio_url})"
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
    """Pick the single message-level reaction from per-deal outcomes."""
    types = {o["outcome"] for o in outcomes}
    if "error" in types:
        return REACTION_ERROR
    if "added" in types:
        return REACTION_ADDED
    if "duplicate" in types and "out_of_scope" not in types:
        return REACTION_DUPLICATE
    if "out_of_scope" in types:
        return REACTION_SKIPPED
    return REACTION_NOT_DEAL


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

    domain = deal.get("domain")
    if domain:
        values["domains"] = [domain]

    description = deal.get("description")
    if description:
        values["description"] = description

    linkedin = deal.get("linkedin_url")
    if linkedin:
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


def _build_inbound_entry_values(
    *,
    step: str,
    source: str | None,
    description: str | None,
    sourcer_member: str | None,
    lead_members: list[str],
) -> dict[str, Any]:
    """Build the entry_values payload for an Inbound Deals entry, dropping
    any keys whose value is None/empty. Attio rejects explicit nulls on
    text attributes with a 400 validation_type error.

    sourcer_member: single Attio workspace_member_id of the Slack poster
                    (always — they sourced the deal into the channel).
    lead_members:   list of Attio workspace_member_ids — any @-mentioned
                    colleagues first, then the poster. Empty if nobody
                    is mappable.
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


def _build_inbound_description(deal: dict[str, Any], permalink: str) -> str:
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
    if permalink:
        bits.append(f"Slack: {permalink}")

    base = "\n".join(bits)
    extra = deal.get("description")
    if extra:
        base = f"{extra}\n\n{base}" if base else extra
    return base


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
