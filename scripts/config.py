"""Constants and configuration for the deal intake bot.

Secrets come from environment variables (set via GitHub Actions secrets).
IDs and slugs are non-sensitive and live here.
"""

import os

# --- Secrets (from env) ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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
    "Database (Specter, Dealroom)",
    "Demo Day",
    "Sector Research",
    "Accelerator / Incubator",
    "University",
    "Founder Network",
    "Portfolio Founder",
    "Advisory / Broker Firm",
    "Active Sourcing",
)

# --- LLM ---
CLAUDE_MODEL = "claude-opus-4-7"

# --- Ingest ---
INGEST_LOOKBACK_SECONDS = 3600       # 1 hour window
INGEST_MESSAGE_LIMIT = 50

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
