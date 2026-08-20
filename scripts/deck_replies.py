"""Files pitchdecks that arrive as a thread reply rather than on the
original deal message.

A deck often shows up after the deal was already staged: Laetizia replies
in the bot's thread with the PDF, or the founder sends it on a few days
later. Ingest itself only looks at top-level messages, so those replies
were previously invisible.

Which company a reply belongs to is recovered from the bot's own threaded
reply, which ends every deal line with the Attio company URL:

    ✅ Acme · Pre-seed · €2M · healthtech (https://app.attio.com/.../record/<id>)

That keeps this pass stateless, the same trick outreach_replies.py uses to
map digest rows back to entry_ids. If the bot's reply names more than one
company, the deck is left alone — filing it against the wrong record is
worse than not filing it.

Runs right after ingest on the same schedule.
"""

from __future__ import annotations

import re
import sys
import time
import traceback
from datetime import datetime, timezone

from attio_client import AttioClient
from config import (
    DEAL_PIPELINE_LIST_ID,
    DECK_LINK_HOSTS,
    DECK_REPLY_MAX_HISTORY,
    DECK_REPLY_PROCESSED_REACTIONS,
    DECK_REPLY_THREAD_LOOKBACK_DAYS,
    INBOUND_DEALS_LIST_ID,
    INGEST_LOOKBACK_SECONDS,
    NAME_FUZZY_THRESHOLD,
    REACTION_DECK_CLARIFY,
    REACTION_DECK_ERROR,
    REACTION_DECK_FILED,
    REACTION_DECK_NOTHING_NEW,
)
from ingest import _collect_message_files, _upload_attachments_to_company
from rapidfuzz import fuzz
from slack_client import SlackClient

# The company record id out of an Attio URL the bot posted.
_RECORD_URL_RE = re.compile(
    r"/objects/companies/record/([0-9a-fA-F-]{36})"
)

# Slack renders links as <url> or <url|label>; grab the url part.
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>\s]+)(?:\|[^>]*)?>")
_BARE_URL_RE = re.compile(r"https?://[^\s<>|]+")

# Sort key fallback for entries Attio returned without a created_at.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def main() -> int:
    slack = SlackClient()
    attio = AttioClient()

    oldest = int(time.time() - DECK_REPLY_THREAD_LOOKBACK_DAYS * 86400)
    try:
        history = slack.fetch_messages_since(oldest, DECK_REPLY_MAX_HISTORY)
    except Exception as e:
        print(f"[decks] failed to fetch Slack history: {e}")
        attio.close()
        return 1

    threads = _threads_with_recent_replies(history)
    print(
        f"[decks] {len(history)} message(s) in the last "
        f"{DECK_REPLY_THREAD_LOOKBACK_DAYS}d, {len(threads)} thread(s) with "
        "recent replies"
    )

    applied = 0
    for parent in threads:
        try:
            applied += _process_thread(slack, attio, parent)
        except Exception as e:
            print(f"[decks] thread {parent.get('ts')} failed: {e}")
            traceback.print_exc()

    print(f"[decks] done. Filed decks from {applied} reply/replies.")
    attio.close()
    return 0


def _threads_with_recent_replies(history: list[dict]) -> list[dict]:
    """Thread parents worth opening: they have replies, and the newest reply
    landed inside the ingest lookback window.

    `latest_reply` comes back on threaded parents in conversations.history,
    so this filter costs nothing and keeps the run from re-fetching every
    thread of the last 30 days on every 5-minute tick.
    """
    cutoff = time.time() - INGEST_LOOKBACK_SECONDS
    out: list[dict] = []
    for msg in history:
        if SlackClient.is_thread_reply(msg):
            continue
        if not msg.get("reply_count"):
            continue
        # Only threads rooted in a human deal message. The bot's own posts
        # (the outreach digest, the promotion summary) carry no deal for a
        # deck to attach to, and their replies belong to other handlers.
        if SlackClient.is_from_bot(msg):
            continue
        latest = msg.get("latest_reply")
        if not latest:
            continue
        try:
            if float(latest) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        out.append(msg)
    out.sort(key=lambda m: float(m.get("ts", "0")))
    return out


def _process_thread(
    slack: SlackClient, attio: AttioClient, parent: dict
) -> int:
    """Handle every unprocessed deck-bearing reply in one thread. Returns
    how many replies were applied."""
    thread_ts = parent.get("ts")
    if not thread_ts:
        return 0
    try:
        messages = slack.fetch_thread_replies(thread_ts)
    except Exception as e:
        print(f"[decks] couldn't fetch thread {thread_ts}: {e}")
        return 0

    candidates = [
        m
        for m in messages
        if m.get("ts") != thread_ts
        and not SlackClient.is_from_bot(m)
        and not SlackClient.has_processed_reaction(m, DECK_REPLY_PROCESSED_REACTIONS)
        and (m.get("files") or _deck_link_in(m.get("text") or ""))
    ]
    if not candidates:
        return 0

    company_ids = _company_ids_from_bot_replies(messages)
    applied = 0
    for reply in candidates:
        ts = reply["ts"]
        try:
            if not company_ids:
                print(
                    f"[decks] reply {ts}: no Attio company in the thread's "
                    "bot reply — nothing to file against"
                )
                slack.post_thread_reply(
                    thread_ts,
                    "📎 Got a deck here, but I can't tell which Attio "
                    "company it belongs to — this thread has no staged deal.",
                )
                slack.add_reaction(ts, REACTION_DECK_CLARIFY)
                continue
            if len(company_ids) == 1:
                company_id = next(iter(company_ids))
            else:
                # Several deals in one thread. The filenames and the reply
                # text usually say which one ("AeroSilicon_Pitch.pdf"), so
                # try to name it before giving up.
                company_id = _disambiguate_company(
                    attio, company_ids, reply
                )
            if not company_id:
                print(
                    f"[decks] reply {ts}: thread names "
                    f"{len(company_ids)} companies and nothing in the reply "
                    "picks one — skipping"
                )
                names = _company_names(attio, company_ids)
                listed = ", ".join(sorted(names.values())) or "several deals"
                slack.post_thread_reply(
                    thread_ts,
                    f"📎 Got a deck here, but this thread covers {listed} and "
                    "I can't tell which one it belongs to. Name the company "
                    "in the reply (or in the filename) and I'll file it.",
                )
                slack.add_reaction(ts, REACTION_DECK_CLARIFY)
                continue
            if _apply_deck_reply(slack, attio, reply, company_id):
                slack.add_reaction(ts, REACTION_DECK_FILED)
                applied += 1
            else:
                slack.add_reaction(ts, REACTION_DECK_NOTHING_NEW)
        except Exception as e:
            print(f"[decks] reply {ts} failed: {e}")
            traceback.print_exc()
            try:
                slack.add_reaction(ts, REACTION_DECK_ERROR)
            except Exception:
                pass
    return applied


def _company_names(
    attio: AttioClient, company_ids: set[str]
) -> dict[str, str]:
    """{record_id: company name} for the companies a thread mentions."""
    out: dict[str, str] = {}
    for cid in company_ids:
        record = attio.get_record("companies", cid)
        name = AttioClient.company_name(record or {})
        if name:
            out[cid] = name
    return out


def _norm_for_match(s: str) -> str:
    """Lowercase alphanumerics only, so "AeroSilicon_Pitch.pdf" and
    "Aerosilicon" line up."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_needles(name: str) -> list[str]:
    """The strings worth matching a company name by. Stealth deals are named
    "Stealth (Max Grollmann)" by ingest's naming heuristic, so the founder
    inside the parens is its own candidate — otherwise the literal "Stealth"
    prefix drags the score below threshold when someone names the founder.
    """
    out = [name]
    inner = re.findall(r"\(([^)]+)\)", name)
    out.extend(inner)
    return out


def _name_matches(name: str, haystack: str) -> bool:
    for candidate in _match_needles(name):
        needle = _norm_for_match(candidate)
        # Short names ("Ai", "Xo") match almost anything under partial_ratio.
        if len(needle) < 5:
            continue
        if fuzz.partial_ratio(needle, haystack) >= NAME_FUZZY_THRESHOLD:
            return True
    return False


def _disambiguate_company(
    attio: AttioClient, company_ids: set[str], reply: dict
) -> str | None:
    """Pick which of a thread's companies a reply's deck belongs to, using
    the filenames and the reply text. Returns None unless exactly one
    company matches — a wrong guess files a deck on the wrong record.
    """
    names = _company_names(attio, company_ids)
    if not names:
        return None
    haystack = _norm_for_match(
        (reply.get("text") or "")
        + " "
        + " ".join(f.get("name") or "" for f in (reply.get("files") or []))
    )
    if not haystack:
        return None

    matches = []
    for cid, name in names.items():
        if _name_matches(name, haystack):
            matches.append((cid, name))

    if len(matches) == 1:
        cid, name = matches[0]
        print(f"[decks] resolved deck to {name!r} ({cid}) from the filenames")
        return cid
    return None


def _apply_deck_reply(
    slack: SlackClient, attio: AttioClient, reply: dict, company_id: str
) -> bool:
    """Upload the reply's files to the company and record a deck link on the
    company's Inbound / Pipeline entries. True if anything was written."""
    attachments = _collect_message_files(slack, reply)
    uploaded = _upload_attachments_to_company(attio, company_id, attachments)

    link = _deck_link_in(reply.get("text") or "")
    updated = _record_deck_link(attio, company_id, link) if link else 0

    if not uploaded and not updated:
        print(
            f"[decks] reply {reply['ts']}: nothing new for company "
            f"{company_id} (already filed)"
        )
        return False

    parts = []
    if uploaded:
        parts.append(f"filed {uploaded} file(s)")
    if updated:
        parts.append(f"set Pitchdeck on {updated} entry/entries")
    print(f"[decks] reply {reply['ts']}: {', '.join(parts)} for {company_id}")
    return True


def _record_deck_link(attio: AttioClient, company_id: str, link: str) -> int:
    """Write `link` to the newest Inbound Deals entry and, if the deal has
    already been promoted, the newest Deal Pipeline entry too. Returns how
    many entries were updated.

    Promote only sets Pitch Deck when it creates the Pipeline entry, so a
    deck arriving after promotion would otherwise never reach the list
    Laetizia actually works in.
    """
    updated = 0
    for list_id, slug in (
        (INBOUND_DEALS_LIST_ID, "pitchdeck"),
        (DEAL_PIPELINE_LIST_ID, "pitch_deck"),
    ):
        entry = _newest_entry_for_company(attio, list_id, company_id)
        if not entry:
            continue
        current = AttioClient.entry_text_value(entry, slug)
        if not _should_overwrite(current, link):
            continue
        entry_id = AttioClient.entry_id(entry)
        if not entry_id:
            continue
        try:
            attio.update_list_entry(list_id, entry_id, {slug: link})
            updated += 1
        except Exception as e:
            print(f"[decks] failed setting {slug} on {entry_id}: {e}")
    return updated


def _should_overwrite(current: str | None, new: str) -> bool:
    """Fill a blank field only. An existing value was either entered by hand
    or captured from an earlier message, and a late reply is no reason to
    overwrite it."""
    return not current


def _newest_entry_for_company(
    attio: AttioClient, list_id: str, company_id: str
) -> dict | None:
    entries = attio.find_list_entries_for_company(list_id, company_id)
    if not entries:
        return None
    return max(
        entries,
        key=lambda e: AttioClient.entry_created_at(e) or _EPOCH,
    )


def _company_ids_from_bot_replies(messages: list[dict]) -> set[str]:
    """Every distinct Attio company the bot named in this thread."""
    out: set[str] = set()
    for m in messages:
        if not SlackClient.is_from_bot(m):
            continue
        out.update(_RECORD_URL_RE.findall(m.get("text") or ""))
    return out


def _deck_link_in(text: str) -> str | None:
    """First URL in the text that sits on a known deck host."""
    if not text:
        return None
    urls = _SLACK_LINK_RE.findall(text) or _BARE_URL_RE.findall(text)
    for url in urls:
        cleaned = url.rstrip(").,;")
        lowered = cleaned.lower()
        if any(h in lowered for h in DECK_LINK_HOSTS):
            return cleaned
    return None


if __name__ == "__main__":
    sys.exit(main())
