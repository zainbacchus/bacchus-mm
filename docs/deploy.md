# Deploying bacchus-mm to fly.io

2026-07-17 (DEPLOY). Goal: the bot trades 24/7 on a fly.io Machine instead of a
laptop (a closed lid = hours of blackout). Everything is already in the repo:

- `Dockerfile` — uv-locked, non-root image (`uv sync --frozen --no-dev`)
- `fly.toml` — app definition: 1× shared-cpu-1x/256MB Machine, 1GB volume at
  `/app/data`, internal `/health` machine check, `always` restart policy
- `/health` endpoint in the bot (auto-enabled by the `HEALTH_PORT` env var)
- startup clock-skew check (RSA-PSS auth signs with local-ms timestamps — a
  skewed VPS clock fails auth opaquely; logged loudly, never blocks)

No public IP is allocated: there is no `[[services]]` block in fly.toml, so the
bot is a pure worker reachable only inside fly's private network. The health
check is a top-level machine check, not a public service.

Assumed: you're on the laptop that has this repo and your Kalshi credentials.
Nothing below requires Docker on the laptop — fly builds images remotely.

---

## 1. Install flyctl and log in (one time)

```bash
brew install flyctl          # macOS; or: curl -L https://fly.io/install.sh | sh
fly auth signup              # new to fly.io — creates the account (browser opens)
# or, if you already have an account:
fly auth login
```

Requires a credit card on file; cost is ~$2/mo (see §10).

## 2. Create the app

```bash
cd /path/to/bacchus-mm
fly apps create bacchus-mm
```

App names are globally unique. If `bacchus-mm` is taken, pick another name,
create that instead, and edit the `app = "..."` line in `fly.toml` to match.

## 3. Create the data volume (same region as primary_region)

The volume holds `bacchus.db` + JSONL logs and survives redeploys and crashes.
It must live in the same region as `primary_region` in fly.toml (`iad`):

```bash
fly volumes create bacchus_data --region iad --size 1
```

(1 GB is ~60× the observed 4-day footprint; volumes can be extended later with
`fly volumes extend`.)

## 4. Set secrets

Secrets are env vars injected at runtime; they never enter the image. Mapping
from `.env.example`:

| `.env.example` var        | On fly? | How |
|---------------------------|---------|-----|
| `KALSHI_ENV`              | not a secret — already `demo` in fly.toml `[env]` | edit fly.toml to change |
| `KALSHI_API_KEY_ID`       | **yes** | `fly secrets set KALSHI_API_KEY_ID=your-key-id` |
| `KALSHI_PRIVATE_KEY_PATH` | **no** — laptop-only; containers have no key file | — |
| `KALSHI_PRIVATE_KEY`      | **yes** (takes precedence over the path, per `.env.example`) | multiline trick below |

Set them (demo keys first — from https://demo.kalshi.co account → API keys):

```bash
fly secrets set KALSHI_API_KEY_ID=your-key-id-here

# Multiline PEM trick: the loader accepts real newlines (and the one-line
# \n-escaped form from .env.example), so a straight $(cat ...) works:
fly secrets set KALSHI_PRIVATE_KEY="$(cat /path/to/kalshi-demo-private-key.pem)"
```

Verify they're set (values stay hidden):

```bash
fly secrets list
```

## 5. Deploy

```bash
fly deploy
```

First build takes a few minutes (remote builder). Done when it says
`v0 deployed` / machine `started`.

## 6. Verify

```bash
fly logs                      # look for: clock skew line, "session …: env=demo …",
                              #           "health endpoint listening on :8080/health"
fly checks list               # the "health" check should go green after grace period
fly machine status            # state: started
# (slim image has no curl — use python:)
fly ssh console -C "python -c \"import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health').read().decode())\""
# → {"mode": "run", "live": false, "halted": false, "uptime_s": …,
#    "last_event_age_s": …, "markets_active": …, "cumulative_pnl": …, "version": …}
# (when unhealthy it tracebacks with "HTTP Error 503" — that's the check
#  reporting an unready bot, not a probe failure)
```

`/health` returns **503** (not 200) when the bot is halted or no event has
fired for 300s — that's the check doing its job, not a bug. Note: failing
checks show up in `fly checks list` and gate deploys, but do **not** restart
the Machine by themselves; restarts come from the `[[restart]]` policy when
the process exits (a dead internal task → `supervise()` → clean exit →
restart). A process that wedges without exiting would sit red in
`fly checks list` — restart it by hand: `fly machine restart`.

If the bot can't write to `/app/data` on first boot (only seen with
pre-existing root-owned volumes): `fly ssh console -C "chown -R bacchus:bacchus /app/data"`.

## 7. Going live on prod — the two-key gate, container edition

Stay on demo until `analyze markouts` says you're earning spread. Locally the
prod gate is `live.enabled: true` in `config.local.yaml` **and** `--live`.
`config.local.yaml` is deliberately excluded from the image, so on fly the
config-file half becomes an env var:

1. Generate **prod** API keys at https://kalshi.com → account → API keys, then
   replace the secrets:
   ```bash
   fly secrets set KALSHI_API_KEY_ID=prod-key-id
   fly secrets set KALSHI_PRIVATE_KEY="$(cat /path/to/kalshi-prod-private-key.pem)"
   ```
2. Edit `fly.toml`: `[env] KALSHI_ENV = "prod"`, and
   `[processes] app = "bacchus-mm run --live"`.
3. Set the config half of the gate:
   ```bash
   fly secrets set BACCHUS_LIVE_ENABLED=1
   ```
4. Prove the order plumbing on the live machine (1-contract $0.01 round trip):
   ```bash
   fly deploy   # picks up the fly.toml edits; bot starts live
   fly ssh console -C "bacchus-mm selftest --live"
   ```
   To revert to demo at any time: `fly secrets unset BACCHUS_LIVE_ENABLED`,
   set `KALSHI_ENV = "demo"` and `app = "bacchus-mm run"` in fly.toml,
   `fly deploy`.

Note the order-group fail-closed rule still applies: on prod+live the bot
refuses to trade if Kalshi's order-group creation fails (`startup_aborted`).

## 8. One-off commands on the live machine

The machine's working directory is `/app` and data is `/app/data`, so every
CLI works as-is:

```bash
fly ssh console -C "bacchus-mm analyze summary"
fly ssh console -C "bacchus-mm analyze markouts --hours 48"
fly ssh console -C "bacchus-mm equity"
fly ssh console -C "bacchus-mm halt-clear"    # re-arm after a kill-switch halt
fly ssh console                               # interactive root shell, exit to leave
```

These are read-only or tiny (WAL SQLite allows concurrent readers) and are
safe while the bot runs. Do **not** start a second `run`/`observe` — the
single-instance flock will refuse (and two traders on one account is the bug
the flock exists to prevent).

## 9. Redeploys, upgrades, rollback

```bash
# after git pull / edits (uv lock if dependencies changed):
fly deploy                    # rolling: old machine stops cleanly (60s kill_timeout),
                              # new one starts, health check gates the release
fly releases                  # list releases + image tags
fly deploy --image registry.fly.io/bacchus-mm:deployment-<OLD-TAG>   # rollback
```

State is on the volume, so redeploys never lose fills/PnL/marks. The old
machine's shutdown cancels its resting orders and the new machine's startup
sweep cancels any stragglers on managed tickers — brief overlap is safe.

## 10. Cost

Verified against https://fly.io/docs/about/pricing/ (2026-07-17):

- shared-cpu-1x, 256MB, running 24/7: **$1.94/mo**
- 1GB volume: **$0.15/mo**
- bandwidth: websocket/REST trickle, effectively free at these rates

**≈ $2.10/mo all-in** (call it $2–5 with headroom). Snapshots: daily
auto-snapshots of the volume are on by default; first 10GB of snapshot storage
is free.

## 11. The laptop becomes a read-only analysis station

Once fly runs the bot, never run `run`/`observe` locally against the same
Kalshi account. For analysis, either run reports on the machine (§8) or pull a
consistent snapshot of the DB home:

```bash
# Online-consistent snapshot via SQLite's backup API (safe while bot writes):
fly ssh console -C "python -c \"import sqlite3; s=sqlite3.connect('/app/data/bacchus.db'); d=sqlite3.connect('/app/data/snap.db'); s.backup(d); d.close(); s.close()\""
fly sftp get /app/data/snap.db data/fly-bacchus.db
fly ssh console -C "rm /app/data/snap.db"
```

Then point analysis at the copy (`data/fly-bacchus.db`) rather than the live
`data/bacchus.db`. `bacchus-mm markets` also still works locally — it needs no
credentials and places no orders.

## 12. Ops cheat sheet

| Symptom | Meaning | Action |
|---------|---------|--------|
| `CLOCK SKEW` in logs / `clock_skew_warning` event | host clock >2s off Kalshi | fly's host NTP usually self-heals; auth errors would follow if real — restart machine, escalate if persistent |
| `/health` 503, `halted: true` | kill switch tripped (HALTED marker on volume) | review logs/`analyze incidents`, then `halt-clear` (§8); next auto-restart resumes |
| `/health` 503, big `last_event_age_s` | event loop wedged | `fly machine restart`, then pull logs |
| machine restarting in a loop right after a halt | expected: `[[restart]] policy = "always"` + HALTED marker refusing to trade | `halt-clear` and it recovers by itself |
| deploy fails health gate | bot didn't come up within 120s grace | `fly logs` — usually credentials or a bad config edit |
