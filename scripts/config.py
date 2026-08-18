"""Constants and configuration for the deal intake bot.

Secrets come from environment variables (set via GitHub Actions secrets).
IDs and slugs are non-sensitive and live here.
"""

import json
import os

# --- Secrets (from env) ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ATTIO_API_KEY = os.environ.get("ATTIO_API_KEY", "")

# --- Slack ---
SLACK_CHANNEL_ID = "C0ATPSB31NY"

# Reactions used to mark a message as processed. Any of these on a message
# means "skip — already handled".
REACTION_ADDED = "white_check_mark"       # ✅ added to Inbound Deals
REACTION_RESURFACE = "t-rex"              # 🦖 Companies match but no recent activity; treated as fresh
REACTION_SKIPPED = "fast_forward"         # ⏭️ out of scope
REACTION_DUPLICATE = "repeat"             # 🔁 active duplicate (recent Inbound or active Pipeline)
REACTION_PASSED_RECENT = "-1"             # 👎 Pipeline Passed/Lost ≤100d ago
REACTION_NOT_DEAL = "shrug"               # 🤷 not a deal
REACTION_ERROR = "warning"                # ⚠️ processing error

PROCESSED_REACTIONS = {
    REACTION_ADDED,
    REACTION_RESURFACE,
    REACTION_SKIPPED,
    REACTION_DUPLICATE,
    REACTION_PASSED_RECENT,
    REACTION_NOT_DEAL,
    REACTION_ERROR,
}

# Reactions used to mark a reply to the outreach-chase digest as processed
# (distinct set from PROCESSED_REACTIONS above, which is about ingest's
# top-level-message dedupe — these apply to thread replies instead).
REACTION_REPLY_APPLIED = "white_check_mark"    # ✅ at least one item was applied to Attio
REACTION_REPLY_SKIPPED = "fast_forward"        # ⏭️ every item in the reply was "skip"
REACTION_REPLY_CLARIFY = "grey_question"       # ❓ needs clarification — nothing applied
REACTION_REPLY_ERROR = REACTION_ERROR          # ⚠️ a write failed / unhandled exception

REPLY_PROCESSED_REACTIONS = {
    REACTION_REPLY_APPLIED,
    REACTION_REPLY_SKIPPED,
    REACTION_REPLY_CLARIFY,
    REACTION_REPLY_ERROR,
}

# --- Attio ---
ATTIO_API_BASE = "https://api.attio.com/v2"

# Lists
INBOUND_DEALS_LIST_ID = "a3827d7c-2e9f-42ea-95e5-8ffce77a0d0c"
INBOUND_DEALS_LIST_SLUG = "inbound_deals_5"

DEAL_PIPELINE_LIST_ID = "1289dda1-ecd3-4d7b-b8d9-46335139aa5d"
DEAL_PIPELINE_LIST_SLUG = "vc_deal_flow_4"

# Parent object for both lists
PARENT_OBJECT = "companies"

# --- Stage / scope ---
# In scope by intent: Angel / Pre-seed / Seed.
# A deal is only marked OUT of scope when the stage is *explicitly* Series A
# or later. Missing / "Unknown" stages are treated as in-scope (working
# assumption: if a stage isn't stated, the deal is most likely an early
# round). The deal is added to Inbound Deals with stage left blank.
IN_SCOPE_STAGES = {"Angel", "Pre-seed", "Seed"}
OUT_OF_SCOPE_STAGES = {"Series A", "Series B", "Series C"}
ALL_STAGES = IN_SCOPE_STAGES | OUT_OF_SCOPE_STAGES

# Step values on Inbound Deals entries
STEP_NEW = "New"
STEP_ADD_TO_PIPELINE = "Add to pipeline"
STEP_NOT_RELEVANT = "Not relevant"
STEP_ADDED = "Added"
STEP_DUPLICATE = "Duplicate"
STEP_PASSED_RECENT = "Passed (<100 days)"
STEP_NEW_RESURFACING = "New (resurfacing)"

# Deal Pipeline status on creation. The api_slug for "Status" on Deal
# Pipeline is `stage`; valid options include New, To qualify, Outreach,
# Intro Call, etc. We default to Outreach so that promoted deals
# immediately surface in the team's "do something with these" view.
PIPELINE_DEFAULT_STAGE = "Outreach"

# --- Outreach chase funnel (a sub-sequence of `stage`, not a separate field) ---
# These titles must match the Deal Pipeline "Status" option titles exactly —
# note the stray space before "Warm" in the Partner Attempt option, that's
# intentional, copied verbatim from Attio.
STAGE_OUTREACH = "Outreach"
STAGE_FOLLOW_UP_1 = "Follow Up 1"
STAGE_FOLLOW_UP_2 = "Follow Up 2"
STAGE_PARTNER_WARM_INTRO = "Partner Attempt/ Warm Intro"
STAGE_TO_PASS = "To Pass"

OUTREACH_FUNNEL_STAGES = {
    STAGE_OUTREACH,
    STAGE_FOLLOW_UP_1,
    STAGE_FOLLOW_UP_2,
    STAGE_PARTNER_WARM_INTRO,
}

OUTREACH_INITIAL_DAYS = 3      # Outreach -> due for Follow Up 1
FOLLOW_UP_CADENCE_DAYS = 3     # review_date = last_chased + this, while chasing FU1/FU2
PARTNER_NUDGE_DAYS = 7         # "consider passing" nudge threshold at Partner Attempt/Warm Intro

# Marker text identifying an outreach-chase digest post, used both for
# same-day dedupe (outreach_chase.py) and to find prior digest threads to
# check for replies (outreach_replies.py). Centralized here (rather than
# living in outreach_chase.py) so both modules can import it without a
# circular import between them.
DIGEST_MARKER = "🐢 Outreach follow-ups"

# First names used to detect "tried via <colleague>" as a Partner
# Attempt/Warm Intro signal in reply_parser.py — e.g. "tried via Adrian" or
# "via Rasmus" both mean partner attempt, not just the literal word
# "partner". Kept as an explicit list (rather than derived from the dict
# below) since matching needs first names, not Slack/Attio IDs.
COLLEAGUE_FIRST_NAMES = (
    "Laetizia",
    "Pranav",
    "Shrey",
    "Matilda",
    "Rockman",
    "Adrian",
    "Rasmus",
    "Nicole",
)

# The sourcing_channel option for deals surfaced through a sourcing
# database / AI sourcing tool (Evertrace, Specter, Dealroom, ...). Kept as
# a named constant because ingest and promote both special-case it: the
# Inbound source text carries only the tool name (no "(channel)" suffix),
# and promote re-infers the channel from that name via SOURCING_DATABASE_TOOLS.
DATABASE_SOURCING_CHANNEL = "Database (Evertrace, Specter, Dealroom, etc.)"

# Tool names that, when they appear in the Inbound source text, imply the
# Database sourcing channel above.
SOURCING_DATABASE_TOOLS = ("Evertrace", "Specter", "Dealroom")

# Valid `sourcing_channel` select options on Deal Pipeline. The LLM picks
# one when the message makes the channel clear; promote.py maps the picked
# value (case-insensitive) to one of these. Anything else is dropped.
PIPELINE_SOURCING_CHANNELS = (
    "Ecosystem / AI Campus",
    "VC",
    "Angel",
    "Personal Network",
    "Conference / Event",
    "LinkedIn",
    "Cold Email (Inbound)",
    DATABASE_SOURCING_CHANNEL,
    "Demo Day",
    "Sector Research",
    "Accelerator / Incubator",
    "University",
    "Founder Network",
    "Portfolio Founder",
    "Advisory / Broker Firm",
    "Active Sourcing",
)

# --- LLM (via OpenRouter) ---
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-5-mini"

# --- Ingest ---
# Lookback window for fetching messages from Slack on each cron fire.
# 4 hours generously covers GitHub Actions cron drift (which can run
# 1-2 hours late even on public repos under load). Already-processed
# messages are filtered out by their bot reactions, so revisiting them
# is cheap.
INGEST_LOOKBACK_SECONDS = 14400      # 4 hours
INGEST_MESSAGE_LIMIT = 50

# --- Deck replies (pitchdecks posted as a thread reply) ---
# A deck often arrives after the deal was already staged — Laetizia replies
# in the bot's thread with the PDF, or the founder sends it on later. The
# deck-reply pass scans thread replies and files them against the company
# the thread's root message resolved to.
#
# Threads are looked for across this window, but only threads whose most
# recent reply is inside INGEST_LOOKBACK_SECONDS actually get fetched (see
# deck_replies.py). So the window sets how *old* a thread can be and still
# accept a late deck, not how much work each run does.
DECK_REPLY_THREAD_LOOKBACK_DAYS = 30
DECK_REPLY_MAX_HISTORY = 1000

# Hosts whose links count as a pitch deck / data room when they show up in a
# thread reply. Matched as a domain suffix, case-insensitively. Deliberately
# an allowlist rather than an LLM call: it's one regex on a short reply, and
# a wrong guess would overwrite a good Pitchdeck value.
DECK_LINK_HOSTS = (
    "docsend.com",
    "drive.google.com",
    "docs.google.com",
    "dropbox.com",
    "box.com",
    "onedrive.live.com",
    "1drv.ms",
    "notion.so",
    "notion.site",
    "pitch.com",
    "papermark.io",
    "papermark.com",
    "brieflink.com",
    "figma.com",
)

# --- Dedupe ---
NAME_FUZZY_THRESHOLD = 85
NAME_STOP_WORDS = {"the", "inc", "ltd", "gmbh", "ag", "sa", "llc", "co"}

# A new deal is only marked as "Duplicate" (or "Passed (<100 days)") if
# the matching Company has activity in this window — Inbound entry
# created within DUPLICATE_RECENCY_DAYS, or Pipeline entry created
# within DUPLICATE_RECENCY_DAYS, or Pipeline entry in any active
# (non-terminal) status regardless of age. Otherwise the deal is
# treated as a resurface and added with Step=New + a 🦖 reaction.
DUPLICATE_RECENCY_DAYS = 100

# Pipeline statuses that count as "no longer active". A Pipeline entry in
# only these statuses contributes nothing toward the duplicate signal
# (unless it was created very recently — see _has_recent_terminal_pipeline_entry
# in dedupe.py).
PIPELINE_TERMINAL_STATUSES = frozenset({"Passed", "Lost"})

# --- Known deal-sharing investor contacts ---
# Loaded at runtime from the INVESTOR_CONTACTS_JSON GitHub secret so the
# data is never committed to the public repo. The secret holds a JSON
# array; each entry has:
#   name:    canonical full name (used for the Attio lookup)
#   firm:    firm/fund the person is associated with (None if unknown)
#   aliases: optional list of nicknames the person might be referred to
#            by in Slack (e.g. a short form for a longer full name)
#
# Used at promote time to expand a partial name like "Foo from BarVC"
# into the canonical full name before searching Attio. Matching is
# fuzzy on both name and firm; the firm hint disambiguates when
# several contacts share a first name. If the secret is unset (e.g.
# a fresh clone, local testing) the bot still works — just without
# the expansion shortcut.
def _load_investor_contacts() -> list[dict]:
    raw = os.environ.get("INVESTOR_CONTACTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"WARNING: failed to parse INVESTOR_CONTACTS_JSON: {e}")
        return []
    if not isinstance(data, list):
        print(
            "WARNING: INVESTOR_CONTACTS_JSON must be a JSON array of objects"
        )
        return []
    return [item for item in data if isinstance(item, dict) and item.get("name")]


INVESTOR_CONTACTS: list[dict] = _load_investor_contacts()


# --- Slack user -> Attio workspace member mapping ---
# Used to populate the Sourcer and Deal Lead attributes on Inbound Deals
# entries. Sourcer is always the Slack poster; Deal Lead is any
# @-mentioned colleague(s) first, then the poster. Posters / mentions
# not in this map are simply ignored.
SLACK_USER_TO_ATTIO_MEMBER = {
    "U093USR1DDE": "c54f95b1-48af-4697-b965-60eac9bd1368",  # Laetizia Hellmeister
    "U0ANFPTB41E": "e84ae462-af28-42fe-82fe-4373c8ea9858",  # Pranav Tadikonda
    "U09HRG2EM43": "653d6276-e8ed-46dd-b15e-d82bece3f87a",  # Shrey Mittal
    "U06UJCMF32S": "c9d19f01-8f8b-4697-89a1-c9a7765b3641",  # Matilda Glynn-Henley
    "U086NAN4ZLN": "5e0f7ea0-8e1d-4263-a2e4-7a92c9de9d6b",  # Rockman Law
    "U1K6Y4U59":   "a81dd787-7863-4946-affb-a1ca9b708eb6",  # Adrian Locher
    "U1K8DJ4MD":   "4c651318-ffd6-4fd1-b1fe-8cd6ebb7db20",  # Rasmus Rothe
    "U0AT4FU0U8P": "a7c78bf9-ef9e-4a16-94dc-e446af0aca5e",  # Nicole Büttner
}

# Inverse lookup — used by the daily digest to @-tag the deal lead in Slack.
ATTIO_MEMBER_TO_SLACK_USER = {
    member_id: slack_id
    for slack_id, member_id in SLACK_USER_TO_ATTIO_MEMBER.items()
}
