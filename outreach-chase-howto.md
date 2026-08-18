**Outreach-chase bot — how it works**

Once a day, the bot posts a digest in `#deal-intake` listing every Deal Pipeline entry that's due for its next outreach action — grouped by Deal Lead, so you'll see your own deals under an @-mention. If nothing's due, it just doesn't post that day (no news = no post, not a broken bot).

Each row looks like:
`1. *Acme* — Jane · 8d waiting · LinkedIn ↗`

**Reply directly in that thread** with the row number + what happened, e.g.:

`1 followed up 2 pass 3 skip`

One number per deal you want to update, in any order, all in one message.

**Actions it understands:**
- `followed up` / `followed` / `chased` — logs the chase, moves the deal to the next stage (Outreach → Follow Up 1 → Follow Up 2). If it's already at Follow Up 2 or Partner Attempt, it just logs another chase without moving the stage.
- `partner` / `warm intro` — marks it as a Partner Attempt / Warm Intro attempt. Name who you tried and it'll credit them automatically, e.g. `2 tried via Adrian` or `2 tried their linkedin`.
- `pass` / `passed` — moves the deal straight to To Pass.
- `skip` / `later` / `not yet` — no change, it'll just resurface on a future digest.

**What happens after you reply:** the bot posts a one-line confirmation per row back in the thread and reacts to your message (✅ applied, ⏭️ everything skipped, ❓ something needs clarifying, ⚠️ an error). If it can't confidently match what you wrote, it asks you to reply again rather than guessing — so if you get a ❓, just retry with the row number and one of the keywords above.

You can reply late too — even to an older digest thread — since it always checks the deal's live current stage before applying anything, not whatever the digest assumed at post time.

Nothing to set up on your end. It's fully automated (GitHub Actions), same channel, same reactions convention as the rest of the deal-intake bot.
