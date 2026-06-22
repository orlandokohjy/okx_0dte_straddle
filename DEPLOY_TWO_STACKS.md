# Running two algos side-by-side (two accounts, one VPS)

This guide runs **two fully isolated stacks** on the same host:

| Stack | Account | Branch | Trade gate | Container |
|-------|---------|--------|-----------|-----------|
| **plain**  | account 1 | `main`             | OFF (`TRADE_GATE_ENABLED` unset) | `okx_0dte_straddle` (default) |
| **signal** | account 2 | `feat/trade-gate`  | ON  (`TRADE_GATE_ENABLED=true`)  | `okx_signal` |

Each stack has its own `.env`, `state/`, `logs/`, container name, and compose
project, so you can build/restart/force-liquidate one account without touching
the other. They only ever interact with their own account via their own keys.

> **The single biggest risk is pointing both `.env` files at the same account.**
> If two stacks share an account, the PID singleton lock and the position
> reconciler will fight each other and you'll get orphaned positions. Verify the
> account on each `.env` *before* launching (see step 4).

---

## 1. Create two worktrees (one shared repo)

From the existing clone on the VPS:

```bash
cd /opt/okx_0dte_straddle           # your existing clone
git fetch origin

# Stack A — plain algo (account 1), tracks main
git worktree add /opt/okx-plain  main

# Stack B — signal-gated algo (account 2), tracks feat/trade-gate
git worktree add /opt/okx-signal feat/trade-gate
```

Worktrees share one object store, so `git pull` in each dir updates only that
branch — you can ship a fix to one algo without disturbing the other.

---

## 2. Configure the PLAIN stack (account 1)

```bash
cd /opt/okx-plain
cp .env.example .env        # or copy your current live .env
```

Edit `/opt/okx-plain/.env`:

```dotenv
# OKX — ACCOUNT 1 keys
OKX_API_KEY=...acct1...
OKX_API_SECRET=...acct1...
OKX_PASSPHRASE=...acct1...
OKX_FLAG=0                  # 0 = LIVE, 1 = demo
OKX_DOMAIN=https://www.okx.com

# Telegram — keep these distinct from the signal stack so you can tell
# which algo sent an alert (separate bot token OR at least a chat).
TELEGRAM_BOT_TOKEN=...botA...
TELEGRAM_CHAT_ID=...chatA...
TELEGRAM_REPORT_BOT_TOKEN=...reportA...   # optional daily-report channel
TELEGRAM_REPORT_CHAT_ID=...reportChatA...

# Trade gate stays OFF on this stack (default). Do NOT set TRADE_GATE_ENABLED.

# Container name: default is okx_0dte_straddle on main — leave as is.

# First boot on a fresh account: wipe demo state so equity starts clean.
# RESET_STATE_ON_BOOT=true   # set once, auto-disables after first run
```

---

## 3. Configure the SIGNAL stack (account 2)

```bash
cd /opt/okx-signal
cp .env.example .env
```

Edit `/opt/okx-signal/.env`:

```dotenv
# OKX — ACCOUNT 2 keys (MUST be a different account from the plain stack)
OKX_API_KEY=...acct2...
OKX_API_SECRET=...acct2...
OKX_PASSPHRASE=...acct2...
OKX_FLAG=0                  # 0 = LIVE, 1 = demo
OKX_DOMAIN=https://www.okx.com

# Telegram — distinct from the plain stack
TELEGRAM_BOT_TOKEN=...botB...
TELEGRAM_CHAT_ID=...chatB...
TELEGRAM_REPORT_BOT_TOKEN=...reportB...   # optional daily-report channel
TELEGRAM_REPORT_CHAT_ID=...reportChatB...

# Distinct container name so it never collides with the plain stack
CONTAINER_NAME=okx_signal

# --- Trade gate ---
TRADE_GATE_ENABLED=true
# Defaults are usually fine; override only if needed:
# TRADE_GATE_FILE=signals/trade_gate.json   # path INSIDE the container
# TRADE_GATE_MAX_AGE_SEC=900                 # signal freshness budget (15 min)
# TRADE_GATE_MATCH_WEEKDAY=true              # match wd/we day-type
# TRADE_GATE_FAIL_OPEN=false                 # default: skip if unverifiable
# TRADE_GATE_WAIT_SEC=90                     # poll budget for a late publish
# TRADE_GATE_POLL_SEC=3                      # re-read interval
```

The signal mount (`/opt/tft/vsn-vol-forecaster/signals:/app/signals:ro`) is
already in this branch's `docker-compose.yml`. Confirm the host path exists:

```bash
ls -l /opt/tft/vsn-vol-forecaster/signals/trade_gate.json
```

---

## 4. VERIFY each .env hits the intended account (do this before launching)

Run a read-only balance check in each dir; confirm the equity/account matches
what you expect for that account:

```bash
cd /opt/okx-plain  && docker compose -p okx_plain  run --rm algo python diagnose_okx.py
cd /opt/okx-signal && docker compose -p okx_signal run --rm algo python diagnose_okx.py
```

If both print the same account/equity, STOP — your keys are crossed.

---

## 5. Launch both stacks

```bash
cd /opt/okx-plain  && docker compose -p okx_plain  up -d --build
cd /opt/okx-signal && docker compose -p okx_signal up -d --build
```

The `-p` project flag namespaces networks/volumes per stack. Combined with the
distinct `container_name`, the two are fully isolated.

---

## 6. Operate them independently

```bash
# Logs
docker logs -f okx_0dte_straddle      # plain
docker logs -f okx_signal             # signal

# Restart just one
cd /opt/okx-signal && docker compose -p okx_signal restart

# Stop just one
cd /opt/okx-plain  && docker compose -p okx_plain down

# Ship an update to just the signal algo
cd /opt/okx-signal && git pull && docker compose -p okx_signal up -d --build
```

---

## Pre-flight checklist

- [ ] `/opt/okx-plain/.env` → account 1 keys; `/opt/okx-signal/.env` → account 2 keys (verified in step 4)
- [ ] Telegram bot/chat differs between stacks (or distinct prefix)
- [ ] `CONTAINER_NAME=okx_signal` set in the signal `.env`
- [ ] Signal file present: `/opt/tft/vsn-vol-forecaster/signals/trade_gate.json`
- [ ] `TRADE_GATE_ENABLED=true` ONLY on the signal stack
- [ ] Host clock synced (chrony / systemd-timesyncd) — cron + gate freshness depend on accurate UTC
- [ ] Each stack has its own `state/` and `logs/` (guaranteed by separate worktree dirs)
