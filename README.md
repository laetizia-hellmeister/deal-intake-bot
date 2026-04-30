# Deal Intake Bot

A Slack bot that processes deal messages from `#deal-intake` and stages them in Attio for review. Runs entirely on GitHub Actions — no external hosting.

## What it does

- **Ingest (every 5 min):** polls the Slack channel, parses new deal messages with Claude, checks scope (Angel/Pre-seed/Seed only), fuzzy-matches against existing Attio companies, and either skips, flags as duplicate, or stages in the **Inbound Deals** list. Uses Slack reactions (✅ ⏭️ 🔁 🤷 ⚠️) to track processed state — no database needed.
- **Promote (daily 17:00 Europe/Copenhagen):** moves any Inbound Deals entries marked `Add to pipeline` into the main **Deal Pipeline** list and flips their step to `Added`.

## Setup

1. **Slack app scopes** (Bot Token Scopes — reinstall after adding):
   - `channels:history`
   - `chat:write`
   - `reactions:read`
   - `reactions:write`

2. **GitHub secrets** (Settings → Secrets and variables → Actions):
   - `SLACK_BOT_TOKEN` — `xoxb-...`
   - `ANTHROPIC_API_KEY` — `sk-ant-...`
   - `ATTIO_API_KEY` — Attio access token

3. **Enable workflows** under the Actions tab after manual testing passes.

## Manual testing

Do NOT enable cron until these seven steps pass via `workflow_dispatch`:

1. Post a full-info Pre-seed deal → trigger `ingest` → verify reply + reaction + Attio entry.
2. Post the same deal again → expect "already tracked".
3. Post a Series B deal → expect "out of scope".
4. Post casual chatter → expect 🤷 reaction only.
5. Set one Inbound entry's Step to `Add to pipeline`.
6. Trigger `promote` → verify entry lands in Deal Pipeline, Inbound step becomes `Added`.
7. Trigger `promote` again → no duplicates.

## Project structure

```
.github/workflows/
  ingest.yml        # every 5 min
  promote.yml       # daily 17:00 Europe/Copenhagen
scripts/
  ingest.py         # ingest entry point
  promote.py        # promote entry point
  extractor.py      # Claude prompt + JSON parsing
  attio_client.py   # Attio API wrapper
  slack_client.py   # Slack helpers
  dedupe.py         # fuzzy matching
  config.py         # IDs, slugs, constants
requirements.txt
```

## Reactions legend

| Reaction | Meaning |
|---|---|
| ✅ `white_check_mark` | Added to Inbound Deals |
| 🔁 `repeat` | Duplicate — already tracked |
| ⏭️ `fast_forward` | Out of scope (stage) |
| 🤷 `shrug` | Not a deal |
| ⚠️ `warning` | Error during processing — remove reaction to retry |

## Notes

- GitHub cron is best-effort; drift of 5–15 min is normal.
- The promote workflow schedules at both 15:00 and 16:00 UTC and gates on local hour == 17 to handle DST.
- Private repo uses Actions minutes fast at 5-min cadence (~4,500/mo). Public repo is free.
