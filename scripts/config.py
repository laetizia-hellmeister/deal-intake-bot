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
REACTION_SKIPPED = "fast_forward"         # ⏭️ out of scope
REACTION_DUPLICATE = "repeat"             # 🔁 already tracked
REACTION_NOT_DEAL = "shrug"               # 🤷 not a deal
REACTION_ERROR = "warning"                # ⚠️ processing error

PROCESSED_REACTIONS = {
    REACTION_ADDED,
    REACTION_SKIPPED,
    REACTION_DUPLICATE,
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

# Deal Pipeline stage on creation
PIPELINE_STAGE_NEW = "New"

# --- LLM ---
CLAUDE_MODEL = "claude-opus-4-7"

# --- Ingest ---
INGEST_LOOKBACK_SECONDS = 3600       # 1 hour window
INGEST_MESSAGE_LIMIT = 50

# --- Dedupe ---
NAME_FUZZY_THRESHOLD = 85
NAME_STOP_WORDS = {"the", "inc", "ltd", "gmbh", "ag", "sa", "llc", "co"}

# --- Deal Lead mapping (Slack user_id -> Attio workspace_member_id) ---
# When a deal is added to Inbound Deals, the Deal Lead attribute is set to
# the Attio workspace member that matches the Slack poster. If the poster
# isn't in this map, deal_lead is left empty and Attio will fall back to
# its default (the API key owner — i.e. the bot identity).
SLACK_USER_TO_ATTIO_MEMBER = {
    "U093USR1DDE": "c54f95b1-48af-4697-b965-60eac9bd1368",  # Laetizia Hellmeister
}
