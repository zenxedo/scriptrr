# scriptrr3

**One place for the scripts you already run.** Drop a script in and it lives
here — listed, readable, runnable on demand. Add a schedule when you want one.
Give it a lifecycle when it's a standing intention. Nothing is required.

**The loop it owns:** `declare why → run → observe → notify → act → close`

Every recurring job is that loop. Today the pieces live in different tools and
*you* are the glue: cron runs it, a heartbeat service watches it, an alert
channel pings you, a note remembers why. scriptrr3 holds the whole loop as one
contained object — so you can see what a job is *for*, when it *runs*, whether
it's *alive*, what it *last said*, and when it *ends*.

## Principles

- **Everything is opt-in.** A script with no schedule, no state, no alerts is
  not incomplete. It is a first-class citizen.
- **Your scripts stay yours.** Plain files, self-contained, runnable outside
  scriptrr3. It holds intent — never a cage.
- **One loop, done well.** No executors, no orchestration. The job owns its
  lifecycle.
- **Contained and human-legible** is the product. If a feature doesn't serve the
  loop, it doesn't belong.

## Tiers

Each tier is additive. You never *have* to climb.

| Tier | What you add | What you get |
|---|---|---|
| 0 | nothing | the script is kept, listed, runnable on demand |
| 1 | a schedule | it runs on a cron cadence |
| 2 | state + alert + expiry | a self-retiring nudge |
| 3 | judgment (optional) | a job that needs reasoning, not just a check |

## A self-retiring nudge

A nudge is a standing intention: *"I already decided to act when X happens; ping
me when X happens, then stop."* The job owns the whole loop and closes itself
out — no human bookkeeping, no zombie cron that nags forever.

```python
EXPIRES = "2026-10-27"          # hard fallback; "" disables
# ...
if state.get("done"):           # terminal condition already met
    retire("already done")      # no-op + best-effort unschedule
if EXPIRES and today() > EXPIRES:
    retire("expired")

# ...do the check...
if condition_met:
    notify(title, message)      # the actionable ping
    retire("condition met")     # alert once, then disappear
```

The state file is the job's memory; `done` and `EXPIRES` are its off-switch.
Under scriptrr3 the job unschedules itself; under plain cron the same script
just no-ops after expiry, so it stays portable.

## Run it

See `Dockerfile` and `../compose/personal/compose.yaml`. The app serves on
`:8000` (host `:5181`) and executes scripts from its `scripts/` directory.
