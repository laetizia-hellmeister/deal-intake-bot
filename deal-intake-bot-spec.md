# Deal Intake Bot — Build Spec (GitHub Actions version)

A Slack bot that processes deal messages and stages them in Attio for review.
**Runs entirely on GitHub Actions** — no external hosting required.

---

## Goal

A dedicated Slack channel collects deal messages. Every 5 minutes, a GitHub Action polls the channel, parses new messages, checks scope (Angel/Pre-seed/Seed only), fuzzy-matches against existing Attio companies, and either skips, flags as duplicate, or stages in the Inbound Deals list. Once daily at 17:00 Europe/Copenhagen, a second Action promotes any Inbound Deals entries marked "Add to pipeline" into the main Deal Pipeline list.

---

## Architecture

```
GitHub Repo (source of truth)
│
├── Workflow 1: ingest.yml (cron: every 5 min)
│   └── python scripts/ingest.py
│       ├── Fetch recent #deal-intake messages via Slack API
│       ├── Skip messages already reacted to (✅, ⏭️, 🔁, 🤷, ⚠️)
│       ├── Extract deal info with Claude
│       ├── Scope check + dedupe against Attio
│       ├── Create Company + add to Inbound Deals list
│       ├── Post reply in Slack thread
│       └── React to the original message (marks it as processed)
│
└── Workflow 2: promote.yml (cron: daily 17:00 Europe/Copenhagen)
    └── python scripts/promote.py
        ├── Query Inbound Deals for Step = "Add to pipeline"
        ├── For each: add to Deal Pipeline, set Inbound Step = "Added"
        └── Post summary message in Slack channel
```

**Key design decision: use Slack reactions to track state.** After processing a message, the bot adds a reaction. Subsequent polls skip any message that already has a bot reaction. Benefits:
- No database, no cache, no file commits needed
- You see at a glance which messages have been processed
- To reprocess, just remove the reaction manually

---

## Tech stack

- **Language:** Python 3.11
- **Libraries:** `slack-sdk`, `httpx`, `anthropic`, `rapidfuzz`
- **Runtime:** GitHub Actions (ubuntu-latest runners)
- **LLM:** `claude-opus-4-7`

---

## Required GitHub Secrets

Set under **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `ATTIO_API_KEY` | Attio access token |

Non-secret config stays in `config.py` (IDs are fine to commit).

---

## Slack app scopes

You do **NOT** need: Event Subscriptions, Request URL, `message.channels` subscriptions.

You **DO** need these OAuth scopes (Bot Token Scopes):
- ✅ `channels:history` — read messages via API
- ✅ `chat:write` — post replies
- ✅ `reactions:read` — check if message already processed
- ✅ `reactions:write` — add reactions to mark processed

If you added these before, good. If not, add them and **reinstall the app** (OAuth & Permissions page → "Reinstall to Workspace").

---

## Attio structural reference

**Lists**
- Inbound Deals (staging): `a3827d7c-2e9f-42ea-95e5-8ffce77a0d0c` (slug: `inbound_deals_5`)
- Deal Pipeline (main):    `1289dda1-ecd3-4d7b-b8d9-46335139aa5d` (slug: `vc_deal_flow_4`)

**Parent object for both:** `companies`

**Slack channel ID:** `C0ATPSB31NY`

### Companies object — attributes the bot writes
| Field       | api_slug      | Type      | Notes                     |
|-------------|---------------|-----------|---------------------------|
| Name        | `name`        | text      | Required                  |
| Domains     | `domains`     | domain[]  | Unique — used for dedupe  |
| Description | `description` | text      |                           |
| LinkedIn    | `linkedin`    | text      | Full URL                  |

### Inbound Deals — entry attributes
| Field       | api_slug      | Type   | Notes                                            |
|-------------|---------------|--------|--------------------------------------------------|
| Source      | `source`      | text   | Who shared the deal                              |
| Description | `description` | text   | Founders + sector + round + Slack permalink      |
| Step        | `step`        | select | Set to `"New"` on creation                       |

**Step options:** `New`, `Add to pipeline`, `Not relevant`, `Added`

### Deal Pipeline — attributes used when promoting
| Field          | api_slug                  | Type     | Notes                          |
|----------------|---------------------------|----------|--------------------------------|
| Status         | `stage`                   | status   | Set to `"New"`                 |
| Upcoming round | `upcoming_round`          | select[] | Angel / Pre-seed / Seed        |
| Round size     | `upcoming_round_size_eum` | number   | Only if extracted              |

Valid `upcoming_round` values: `Angel`, `Pre-seed`, `Seed`, `Series A`, `Series B`, `Series C`.

---

## Scope rules

**In scope:** `Angel`, `Pre-seed`, `Seed`
**Out of scope:** `Series A`, `Series B`, `Series C`, or `null`/unknown

Out-of-scope deals are skipped — not added to Inbound Deals. Bot posts a threaded reply with reason + reacts ⏭️.

---

## Project structure

```
deal-intake-bot/
├── .github/
│   └── workflows/
│       ├── ingest.yml        # Every 5 min cron
│       └── promote.yml       # Daily 17:00 Europe/Copenhagen cron
├── scripts/
│   ├── ingest.py             # Main entry for ingest workflow
│   ├── promote.py            # Main entry for promote workflow
│   ├── extractor.py          # Claude prompt + JSON parsing
│   ├── attio_client.py       # Thin wrapper over Attio API
│   ├── slack_client.py       # Slack helpers
│   ├── dedupe.py             # Fuzzy matching
│   └── config.py             # All constant IDs from above
├── requirements.txt
├── .gitignore                # .env, __pycache__, *.pyc
└── README.md
```

---

## Flow 1: Ingest (every 5 min)

### Workflow file `.github/workflows/ingest.yml`

```yaml
name: Ingest deals from Slack
on:
  schedule:
    - cron: '*/5 * * * *'
  workflow_dispatch:

concurrency:
  group: ingest
  cancel-in-progress: false

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip
      - run: pip install -r requirements.txt
      - run: python scripts/ingest.py
        env:
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          ATTIO_API_KEY: ${{ secrets.ATTIO_API_KEY }}
```

**Note on GitHub cron timing:** `*/5 * * * *` is best-effort. Under load, runs can drift by 5–15 min. Fine for this use case.

### `scripts/ingest.py` — logic

1. **Fetch** last 50 messages from `C0ATPSB31NY` via `conversations.history`, `oldest = now - 3600` (1h window for safety).
2. **Filter out**:
   - Messages from bots (`subtype == 'bot_message'` or `bot_id` present)
   - Thread replies (`thread_ts` present and `thread_ts != ts`)
   - Messages already reacted by the bot with any of: ✅ `white_check_mark`, ⏭️ `fast_forward`, 🔁 `repeat`, 🤷 `shrug`, ⚠️ `warning`. (Reactions are in the `reactions` field of the message response.)
3. **For each unprocessed message**, call `process_message(msg)`:
   - Extract deal info with Claude
   - If `is_deal == false` → react 🤷, done
   - If stage out of scope → threaded reply + react ⏭️, done
   - Dedupe against Attio Companies
   - If duplicate → threaded reply with link + react 🔁, done
   - Else → upsert Company, add entry to Inbound Deals, threaded reply + react ✅
   - On any exception → threaded reply with error + react ⚠️ (prevents infinite retries)

### Extraction prompt

```
You extract structured deal info from messages sent by a VC about companies
they're considering for investment. Return ONLY valid JSON, no preamble.

Schema:
{
  "is_deal": boolean,
  "company_name": string | null,
  "website": string | null,
  "domain": string | null,
  "linkedin_url": string | null,
  "founders": [ { "name": string, "linkedin": string | null } ],
  "stage": string | null,
  "round_size_eur_m": number | null,
  "sector": string | null,
  "description": string | null,
  "source": string | null
}

Rules:
- is_deal = true only if the message references an identifiable company
  (name or website). Casual chatter, status updates, and off-topic messages → false.
- Do NOT invent data. Missing fields → null.
- Normalize stage to one of: "Angel", "Pre-seed", "Seed", "Series A",
  "Series B", "Series C", "Unknown". Examples:
  "preseed" / "pre seed" / "pre-seed round" → "Pre-seed"
  "seed round" / "seed stage" → "Seed"
  "series a" → "Series A"
- domain = root domain only (strip protocol, www., path, query).
- source = who shared this deal, e.g. "John from Atomico". null if not stated.
- Return only the JSON object, nothing else.
```

### Dedupe logic (`dedupe.py`)

Three signals, any one = duplicate:

1. **Domain match** — Attio query on `domains` attribute. Normalize: lowercase, strip `www.`, protocol, trailing slash.
2. **LinkedIn URL match** — query on `linkedin`. Normalize: strip protocol, trailing slash, query params.
3. **Name fuzzy match** — fetch companies whose name contains the first significant token (skip stop words: `the`, `inc`, `ltd`, `gmbh`, `ag`, `sa`, `llc`, `co`), then score client-side with `rapidfuzz.fuzz.ratio` ≥ 85.

If duplicate, also check whether the matched company appears in Deal Pipeline and/or Inbound Deals (query each list for `parent_record_id = <matched_id>`). Include that context in the Slack reply.

### Slack reply templates

Added:
```
✅ Added to Inbound Deals: *<company>*
Stage: <stage> · Round: €<x>M · Sector: <sector or "?">
<Attio URL>
```

Duplicate:
```
🔁 Already tracked: *<company>*
Found in: <Inbound Deals | Deal Pipeline | both>
<Attio URL>
```

Out of scope:
```
⏭️ Skipped *<company>* — out of scope (stage: <stage or "unknown">)
```

Not a deal: *(no reply, just 🤷 reaction)*

Error:
```
⚠️ Couldn't process this message: <short error>
Marked as reviewed. Remove the ⚠️ reaction to retry.
```

---

## Flow 2: Promote (daily 17:00 Europe/Copenhagen)

### Workflow file `.github/workflows/promote.yml`

```yaml
name: Promote Inbound Deals to Pipeline
on:
  schedule:
    # 17:00 Europe/Copenhagen = 15:00 UTC in summer (CEST), 16:00 UTC in winter (CET).
    # Run at both; the script is idempotent and only acts during local 17:00.
    - cron: '0 15 * * *'
    - cron: '0 16 * * *'
  workflow_dispatch:

jobs:
  promote:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip
      - run: pip install -r requirements.txt
      - run: python scripts/promote.py
        env:
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          ATTIO_API_KEY: ${{ secrets.ATTIO_API_KEY }}
```

### DST handling

GitHub Actions cron runs only in UTC and ignores timezones. Copenhagen is UTC+1 (CET) in winter and UTC+2 (CEST) in summer. To hit 17:00 local year-round, schedule two UTC runs and gate inside the script:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
import sys

now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))
if now_local.hour != 17:
    print(f"Skipping — local hour is {now_local.hour}, not 17")
    sys.exit(0)
```

Only the UTC run that maps to 17:00 local does work. The other exits silently.

### `scripts/promote.py` — logic

1. Check local hour (see above); exit if not 17.
2. Query Inbound Deals: filter `{"attribute": "step", "op": "eq", "value": "Add to pipeline"}`, limit 50.
3. For each entry:
   - `parent_record_id` = company UUID
   - Call `add-record-to-list` on Deal Pipeline: `parent_object=companies`, `parent_record_id=<id>`, `allow_duplicates=false`, `entry_values={"stage": "New"}`
   - On success OR "entry already exists" → `update-list-entry-by-id` on the Inbound entry, `step: "Added"`
   - On other errors → log, continue, don't update step
4. Post summary to `C0ATPSB31NY`:
   ```
   🗓️ Daily promotion: moved N deal(s) to Deal Pipeline.
   • <Company A>
   • <Company B>
   ```
   Skip if N=0.

### Safety constraint
- `promote.py` MUST NOT call update/delete endpoints on Deal Pipeline.
- Only Deal Pipeline endpoint permitted: `add-record-to-list`.

---

## Limits and quotas to watch

| Limit | Value | Impact |
|---|---|---|
| GitHub Actions minutes (public repo) | Unlimited | Use public repo if possible |
| GitHub Actions minutes (private repo) | 2,000/mo free | ~5-min cadence + 1-min runs = ~150/day = ~4,500/mo ⚠️ |
| Slack Web API rate limit | Tier 2 (~50 req/min) | Fine |
| Attio API | 100 req/sec | Fine |
| Anthropic API | Account-dependent | Monitor usage |

**On the private-repo minute cost:** if you keep the repo private, realistic options are:
1. Make the repo public (code only, secrets remain hidden) — recommended if nothing sensitive in code
2. Reduce cadence to every 10 or 15 min
3. Pay for Actions minutes ($0.008/min after the free tier — bot would cost ~$20/mo)

---

## Claude Code prompt

When ready, paste this in Claude Code:

> Build the project described in `deal-intake-bot-spec.md` in this directory.
> Follow the spec exactly — all IDs, slugs, and field mappings are in the spec.
> Build in this order, pausing after each file for me to review:
> 1. `requirements.txt`, `.gitignore`, `README.md`
> 2. `scripts/config.py`
> 3. `scripts/attio_client.py`
> 4. `scripts/slack_client.py`
> 5. `scripts/extractor.py`
> 6. `scripts/dedupe.py`
> 7. `scripts/ingest.py`
> 8. `scripts/promote.py`
> 9. `.github/workflows/ingest.yml`
> 10. `.github/workflows/promote.yml`
> After each file, summarize what was added and wait for me to say "continue".

---

## Manual setup before building

1. **Update Slack app scopes** if needed: `channels:history`, `chat:write`, `reactions:read`, `reactions:write`. Reinstall to workspace.
2. **Create a GitHub repo** (public recommended for free Actions minutes, or plan for the private-repo cost above).
3. **Add three secrets** (Settings → Secrets and variables → Actions):
   - `SLACK_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `ATTIO_API_KEY`
4. **Install Claude Code**: `npm i -g @anthropic-ai/claude-code`
5. Clone the empty repo locally, drop this spec in it, run `claude` inside, paste the prompt above.

---

## Testing strategy

Do NOT enable the cron schedule until manual testing passes.

Test each step via **workflow_dispatch** (the "Run workflow" button in the Actions tab):

1. Post a full-info Pre-seed test deal in `#deal-intake`
2. Manually trigger `ingest.yml` → verify Slack reply + reaction + Attio Inbound entry
3. Post the same deal again → trigger ingest → expect "already tracked" reply
4. Post a Series B deal → trigger ingest → expect "out of scope" reply
5. Post casual chatter ("hey what's for lunch") → trigger ingest → expect 🤷 reaction only
6. Manually set one Inbound entry's Step to "Add to pipeline"
7. Trigger `promote.yml` → verify the entry appears in Deal Pipeline AND Inbound step changes to "Added"

Only after all 7 pass, commit to main and let the cron run.
