# Deal Intake Bot

A Slack bot that processes deal messages from `#deal-intake` and stages them in Attio for review. Runs entirely on GitHub Actions — no external hosting.

## What it does

- **Ingest (every 5 min):** polls the Slack channel, parses new deal messages with an LLM (via OpenRouter), checks scope (Angel/Pre-seed/Seed only), fuzzy-matches against existing Attio companies, and either skips, flags as duplicate, or stages in the **Inbound Deals** list. Uses Slack reactions (✅ ⏭️ 🔁 🤷 ⚠️) to track processed state — no database needed.
- **Pitchdecks:** any file attached to a deal message is uploaded to the company record's **Files** tab in Attio (via `POST /v2/files/upload`, multipart — undocumented but it's what the UI's drag-and-drop uses). PDFs are additionally passed to the LLM so the deck's contents inform extraction. A deck *link* in the message (DocSend, Drive, Notion, ...) goes into the Inbound Deals **Pitchdeck** field; when a file was filed but no link was given, the Slack permalink goes there instead. On promote, a filled **Pitchdeck** carries over to the Deal Pipeline **Pitch Deck** field. Files are only filed when the message contains exactly one deal — on a multi-deal list there's no way to tell which company a file belongs to.
- **Deck replies (every 5 min, right after ingest):** a deck that arrives as a *thread reply* — you replying with the PDF later, or the founder sending it on — gets filed against the company the thread's root message resolved to. The company is recovered from the Attio URL in the bot's own earlier reply, so no state is stored. Threads are considered for 30 days back, but only ones with a reply in the last 4 hours are opened. Skipped when the thread covers more than one company.
- **Promote (daily 17:00 Europe/Copenhagen):** moves any Inbound Deals entries marked `Add to pipeline` into the main **Deal Pipeline** list and flips their step to `Added`.

## Setup

1. **Slack app scopes** (Bot Token Scopes — reinstall after adding):
   - `channels:history`
   - `chat:write`
   - `reactions:read`
   - `reactions:write`
   - `files:read` — required to download pitchdecks. Without it Slack
     returns an HTML sign-in page (HTTP 200) instead of the file.

2. **GitHub secrets** (Settings → Secrets and variables → Actions):
   - `SLACK_BOT_TOKEN` — `xoxb-...`
   - `OPENROUTER_API_KEY` — `sk-or-v1-...`
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
  ingest.yml        # every 5 min (ingest.py + deck_replies.py)
  promote.yml       # daily 17:00 Europe/Copenhagen
scripts/
  ingest.py         # ingest entry point
  deck_replies.py   # decks arriving as thread replies
  promote.py        # promote entry point
  extractor.py      # LLM prompt (OpenRouter) + JSON parsing
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

## Reply-line markers

| Marker | Meaning |
|---|---|
| 📎 | File(s) filed on the company record's Files tab in Attio |
| 🔗 | A deck link was captured into the Pitchdeck field |

On a **thread reply** carrying a deck, the reply itself gets ✅ when the deck was filed, ⏭️ when there was nothing new to file, ❓ when the thread has no staged deal or covers several companies, and ⚠️ on error.

## Notes

- GitHub cron is best-effort; drift of 5–15 min is normal.
- The promote workflow schedules at both 15:00 and 16:00 UTC and gates on local hour == 17 to handle DST.
- Private repo uses Actions minutes fast at 5-min cadence (~4,500/mo). Public repo is free.
