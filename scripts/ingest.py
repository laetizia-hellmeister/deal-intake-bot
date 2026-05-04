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
    IN_SCOPE_STAGES,
    PARENT_OBJECT,
    PIPELINE_STAGE_NEW,
    REACTION_ADDED,
    REACTION_DUPLICATE,
    REACTION_ERROR,
    REACTION_NOT_DEAL,
    REACTION_SKIPPED,
    SLACK_USER_TO_ATTIO_MEMBER,
    STEP_NEW,
)
from dedupe import find_duplicate, location_label
from extractor import extract_deals
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
        attio_member = SLACK_USER_TO_ATTIO_MEMBER.get(slack_user)
        fallback_source = _fallback_source(slack, msg)

        # Process each deal independently; collect a per-deal outcome line.
        outcomes: list[dict[str, Any]] = []
        for deal in deals:
            outcomes.append(
                _process_one_deal(
                    attio=attio,
                    deal=deal,
                    permalink=permalink,
                    attio_member=attio_member,
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


def _process_one_deal(
    *,
    attio: AttioClient,
    deal: dict[str, Any],
    permalink: str,
    attio_member: str | None,
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

    try:
        # Out of scope
        if stage not in IN_SCOPE_STAGES:
            line = (
                f"⏭️ {company_name} — out of scope "
                f"(stage: {stage or 'unknown'})"
            )
            return {"outcome": "out_of_scope", "line": line}

        # Dedupe
        match = find_duplicate(attio, deal)
        if match:
            attio_url = AttioClient.company_web_url(match.company_id or "")
            line = (
                f"🔁 {company_name} — already in {location_label(match)} "
                f"({attio_url})"
            )
            return {"outcome": "duplicate", "line": line}

        # New -> upsert company + add to Inbound Deals
        company_record = _upsert_company(attio, deal)
        company_id = AttioClient.record_id(company_record)
        if not company_id:
            raise RuntimeError("Attio did not return a company record id")

        source = deal.get("source") or fallback_source
        description = _build_inbound_description(deal, permalink)

        entry_values: dict[str, Any] = {
            "source": source,
            "description": description,
            "step": STEP_NEW,
        }
        if attio_member:
            entry_values["deal_lead"] = [
                {
                    "referenced_actor_type": "workspace-member",
                    "referenced_actor_id": attio_member,
                }
            ]

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
        attio_url = AttioClient.company_web_url(company_id)
        line = (
            f"✅ {company_name} · {stage} · {round_str} · {sector} "
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
    """Create or upsert a company. Prefer upsert-by-domain; fall back to create."""
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
        return attio.assert_company(values, matching="domains")
    return attio.create_company(values)


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
    if stage:
        round_str = f" (€{round_size}M)" if round_size else ""
        bits.append(f"Round: {stage}{round_str}")
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
