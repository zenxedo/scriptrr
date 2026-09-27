# scriptrr

**One place for the scripts you already run.** Drop a script in and it lives
here — listed, readable, runnable on demand. Add a schedule when you want one.
Give it a lifecycle when it's a standing intention. Nothing is required.

**The loop it owns:** `declare why → run → observe → notify → act → close`

Every recurring job is that loop. Today the pieces live in different tools and
*you* are the glue: cron runs it, a heartbeat service watches it, an alert
channel pings you, a note remembers why. scriptrr holds the whole loop as one
contained object — so you can see what a job is *for*, when it *runs*, whether
it's *alive*, what it *last said*, and when it *ends*.

## Principles

- **Everything is opt-in.** A script with no schedule, no state, no alerts is
  not incomplete. It is a first-class citizen.
- **Your scripts stay yours.** Plain files, self-contained, runnable outside
  scriptrr. It holds intent — never a cage.
- **One loop, done well.** No executors, no orchestration. The job owns its
  lifecycle.
- **Contained and human-legible** is the product. If a feature doesn't serve the
  loop, it doesn't belong.

## Features

- List, edit, run, stop, and delete `.py` / `.sh` scripts from a web dashboard.
- Per-script **cron scheduling** (APScheduler), with a human-readable next run.
- **Live output** while a script runs, plus a per-run **log** view.
- Descriptions, tags, starring, search, and sortable columns.
- Upload an existing script or create one in the browser.

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
Under scriptrr the job unschedules itself; under plain cron the same script
just no-ops after expiry, so it stays portable.

## Quick start

```sh
docker run -d \
  --name scriptrr \
  -p 8000:8000 \
  -v "$PWD/scripts:/app/scripts" \
  zenxedo/scriptrr:latest
```

Then open <http://localhost:8000/>. Anything you mount at `/app/scripts` is
listed. The container writes `scriptrr.db` and `logs/` under `/app`; mount
those too if you want them to survive a container rebuild:

```sh
  -v "$PWD/scripts:/app/scripts" \
  -v "$PWD/data/scriptrr.db:/app/scriptrr.db" \
  -v "$PWD/data/logs:/app/logs" \
```

### Docker Compose

```yaml
services:
  scriptrr:
    image: zenxedo/scriptrr:latest
    container_name: scriptrr
    ports:
      - "8000:8000"
    volumes:
      # Mount your scripts — anything you put here shows up in the dashboard.
      - ./scripts:/app/scripts
      # Optional: persist metadata (schedules/tags) and run logs.
      - ./data/scriptrr.db:/app/scriptrr.db
      - ./data/logs:/app/logs
    restart: unless-stopped
```

## Script dependencies

The published image contains only the app. If your scripts need extra Python
packages or system tools, build a thin image on top of it:

```dockerfile
FROM zenxedo/scriptrr:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests
USER scriptrr
```

## How scripts run

- `.py` files run with `python3 -u`; `.sh` files run with `bash`.
- A run's stdout/stderr is written to `logs/<script>.log`; the previous log is
  archived first.
- Scheduled runs use each script's saved **default arguments**.
- Scripts are self-contained: they carry their own config and run the same
  under scriptrr, cron, or an interactive shell.

## Security

**scriptrr ships with no authentication.** Anyone who can reach the port can
add, edit, run, and delete scripts — and run arbitrary code on the host with the
container's permissions. Treat it as a trusted-LAN tool:

- Do **not** expose it directly to the internet.
- Put it behind a reverse proxy that provides auth (Authelia, oauth2-proxy,
  basic auth), or reach it over a VPN/Tailscale.
- Mount only the paths your scripts actually need. Mounting the Docker socket
  or a broad host path widens the blast radius.

## License

MIT — see [LICENSE](LICENSE).
