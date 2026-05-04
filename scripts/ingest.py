"""Ingest workflow entry point.

Polls the Slack deal-intake channel, processes any message that has no
bot reaction yet, and either stages it in Attio Inbound Deals or skips it
(with a reaction marking the outcome).
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
from extractor import extract_deal
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
        deal = extract_deal(text, client=anthro)

        # Not a deal -> silent 🤷
        if not deal.get("is_deal"):
            slack.add_reaction(ts, REACTION_NOT_DEAL)
            return

        company_name = deal.get("company_name") or "unknown company"
        stage = deal.get("stage")

        # Out of scope -> threaded reply + ⏭️
        if stage not in IN_SCOPE_STAGES:
            reply = (
                f"⏭️ Skipped *{company_name}* — out of scope "
                f"(stage: {stage or 'unknown'})"
            )
            slack.post_thread_reply(ts, reply)
            slack.add_reaction(ts, REACTION_SKIPPED)
            return

        # Dedupe
        match = find_duplicate(attio, deal)
        if match:
            attio_url = AttioClient.company_web_url(match.company_id or "")
            reply = (
                f"🔁 Already tracked: *{company_name}*\n"
                f"Found in: {location_label(match)}\n"
                f"{attio_url}"
            )
            slack.post_thread_reply(ts, reply)
            slack.add_reaction(ts, REACTION_DUPLICATE)
            return

        # New -> upsert company + add to Inbound Deals
        company_record = _upsert_company(attio, deal)
        company_id = AttioClient.record_id(company_record)
        if not company_id:
            raise RuntimeError("Attio did not return a company record id")

        permalink = slack.permalink(ts) or ""
        source = deal.get("source") or _fallback_source(slack, msg)
        description = _build_inbound_description(deal, permalink)

        entry_values: dict[str, Any] = {
            "source": source,
            "description": description,
            "step": STEP_NEW,
        }

        # Set Deal Lead to the Slack poster (mapped to an Attio member),
        # if we know the mapping. Otherwise Attio defaults it to the API
        # key owner (the bot).
        slack_user = msg.get("user")
        attio_member = SLACK_USER_TO_ATTIO_MEMBER.get(slack_user)
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
        reply = (
            f"✅ Added to Inbound Deals: *{company_name}*\n"
            f"Stage: {stage} · Round: {round_str} · Sector: {sector}\n"
            f"{attio_url}"
        )
        slack.post_thread_reply(ts, reply)
        slack.add_reaction(ts, REACTION_ADDED)

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

    sector = deal.get("sector")
    stage = deal.get("stage")
    round_size = deal.get("round_size_eur_m")

    bits: list[str] = []
    if founder_names:
        bits.append(f"Founders: {founder_names}")
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
