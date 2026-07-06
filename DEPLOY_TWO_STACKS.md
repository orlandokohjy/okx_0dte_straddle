# Running two algos side-by-side (two accounts, one VPS)

Run **two fully isolated stacks** on the VPS (`jiayi@188.166.214.51`):

| Stack | Account | Dir on VPS | Branch | Trade gate | Container |
|-------|---------|-----------|--------|-----------|-----------|
| **plain** (existing live) | account 1 | `~/okx_0dte_straddle` | `main`            | OFF | `okx_0dte_straddle` (default) |
| **signal** (new)          | account 2 (`AITradingAccount2`) | `~/okx-signal` | `feat/trade-gate` | ON | `okx_signal` |

> **Compose is v1 (`docker-compose`, hyphenated) on this VPS — `docker compose`
> (v2, space) is NOT installed.** All commands below use the hyphenated form,
> per `AGENTS.md`.

The plain stack already runs at `~/okx_0dte_straddle`; **leave it untouched.**
We only add a second worktree for the signal stack. Each stack has its own
`.env`, `state/`, `logs/`, container name, and compose project — so you can
build/restart/force-liquidate one account without touching the other.

> **Biggest risk: pointing both `.env` files at the same account.** Two stacks
> on one account make the PID singleton lock and the position reconciler fight
> → orphaned positions. Verify the account on each `.env` BEFORE launching
> (step 4). Account 2's key is whitelisted to the VPS IP `188.166.214.51`.

---

## 1. Add a worktree for the signal stack

The existing live clone stays as-is. From it, add a second working dir on the
gate branch:

```bash
cd ~/okx_0dte_straddle
git fetch origin
git worktree add ~/okx-signal feat/trade-gate
```

One shared object store; `git -C ~/okx-signal pull` updates only the signal
stack, `git -C ~/okx_0dte_straddle pull` only the plain stack.

---

## 2. Drop in the signal stack's .env

Copy the prepared `.env.signal` (account-2 creds, gate ON, `CONTAINER_NAME=okx_signal`)
to the signal worktree as its `.env`:

```bash
# from your laptop, or scp the file up first
scp .env.signal jiayi@188.166.214.51:~/okx-signal/.env
```

Then on the VPS, fill the two blanks I could not set:

```bash
nano ~/okx-signal/.env
#   OKX_PASSPHRASE=...        # the passphrase set when the key was created
#   TELEGRAM_*=...            # optional; use a DIFFERENT bot/chat from plain
#   INITIAL_CAPITAL_USD=...   # account 2's actual starting capital
#   DRY_RUN=true → false      # only after step 4 confirms wiring
```

The signal mount (`/opt/tft/vsn-vol-forecaster/signals:/app/signals:ro`) is
already in this branch's `docker-compose.yml`. Confirm the host path exists:

```bash
ls -l /opt/tft/vsn-vol-forecaster/signals/trade_gate.json
```

---

## 3. (Plain stack — no change needed)

It keeps running at `~/okx_0dte_straddle` on `main` with container
`okx_0dte_straddle` and the default compose project. Don't move it.

---

## 4. VERIFY the signal .env hits account 2 (before any live fill)

The Dockerfile entrypoint is `python main.py`, so a bare
`docker-compose run --rm algo python diagnose_okx.py` would just boot the algo.
You MUST clear the entrypoint:

```bash
cd ~/okx-signal
docker-compose -p okx_signal run --rm --entrypoint "" algo \
    python diagnose_okx.py
```

Confirm it authenticates and shows **account 2's** balance/positions (not
account 1's). `diagnose_okx.py` is read-only — balance + position list, no
orders. If it shows the same account as the plain stack, STOP: your keys are
crossed.

---

## 5. Launch the signal stack

```bash
cd ~/okx-signal
docker-compose -p okx_signal up -d --build
docker-compose -p okx_signal logs -f --tail=200 algo
```

The `-p okx_signal` project flag namespaces networks/volumes; combined with
`CONTAINER_NAME=okx_signal` the two stacks are fully isolated.

> `RESET_STATE_ON_BOOT=true` is set in `.env.signal` so the new account starts
> with clean equity/state. It auto-disables after the first boot.

---

## 6. Operate them independently

```bash
# Logs
docker-compose logs -f --tail=200 algo                       # plain (run from ~/okx_0dte_straddle)
docker-compose -p okx_signal logs -f --tail=200 algo         # signal (run from ~/okx-signal)

# Apply an .env change (v1 footgun: `restart` does NOT re-read .env)
cd ~/okx-signal && docker-compose -p okx_signal up -d --force-recreate algo

# Ship a code update to just the signal algo
cd ~/okx-signal && git pull && docker-compose -p okx_signal up -d --build algo

# Force-liquidate account 2 (clears entrypoint; stop algo first to release lock)
cd ~/okx-signal
docker-compose -p okx_signal stop algo
docker-compose -p okx_signal run --rm --entrypoint "" algo python tools/force_liquidate.py
docker-compose -p okx_signal start algo
```

Never use `--no-cache` on routine code-only deploys (only when
`requirements.txt`/`Dockerfile` change).

---

## Troubleshooting & recurring gotchas (READ THIS before redeploying)

Every past redeploy failure traces back to ONE of the items below. Use the
canonical commands and you will not hit them.

### Canonical redeploy commands (copy these, don't improvise)

```bash
# SIGNAL stack (account 2, branch feat/trade-gate)
cd ~/okx-signal
git pull origin feat/trade-gate
docker-compose -p okx_signal up -d --build algo      # ALWAYS -p okx_signal
docker-compose -p okx_signal logs -f --tail=100 algo # service is "algo", NOT okx_signal

# PLAIN stack (account 1, branch main)
cd ~/okx_0dte_straddle
git pull origin main
docker-compose up -d --build algo
docker-compose logs -f --tail=100 algo
```

### Gotcha A — "container name /okx_signal is already in use" (Conflict)

**Cause:** the signal stack was started once WITH `-p okx_signal` (compose
project `okx_signal`) and later redeployed WITHOUT the flag from
`~/okx-signal` (compose project `okx-signal`, from the dir name). The
container name is pinned to `okx_signal` via `CONTAINER_NAME`, so the two
different *projects* fight over the same *name*.

**Immediate fix** (safe when flat / pre-market):
```bash
docker rm -f okx_signal
docker-compose -p okx_signal up -d --build algo
```

**Permanent fix (do this once so the `-p` flag can never be forgotten):**
pin the project name in the signal stack's `.env` — docker-compose reads it
from `.env`, so bare `docker-compose` and `docker-compose -p okx_signal`
then resolve to the SAME project and can never collide:
```bash
# in ~/okx-signal/.env add:
COMPOSE_PROJECT_NAME=okx_signal
```
Transition (one-time; briefly stops the algo — do it when flat). Order does
NOT matter here because we remove the container BY NAME, which is
project-agnostic (do NOT use `docker-compose down` for the transition — once
`.env` has the new project name, `down` targets the NEW project and misses a
container still running under the OLD project → the same conflict):
```bash
cd ~/okx-signal
echo "COMPOSE_PROJECT_NAME=okx_signal" >> .env   # add the pin (any order)
docker rm -f okx_signal                          # remove BY NAME (project-agnostic)
docker-compose up -d --build algo                # recreates under project okx_signal
```

### Gotcha B — "no such service: okx_signal"

`docker-compose <cmd> okx_signal` fails because compose wants the **service**
name (`algo`), not the container name. Either use the service name, or bypass
compose and use the container name directly:
```bash
docker-compose -p okx_signal logs -f --tail=100 algo   # service name
docker logs -f --tail=100 okx_signal                   # container name (no compose)
```

### Gotcha C — v1 `restart` does NOT re-read `.env`

After editing `.env`, use `up -d --force-recreate algo`, not `restart`.

### Post-deploy verification (confirm the NEW code is actually live)

Check the startup banner (`... logs --tail=100 algo`):
- Every `we_*` session logs `session_skipped_disabled` (weekends OFF).
- `wd_0000/0030/0130` `next_fire` is **Tue–Sat**, never Monday (08:00-roll fix).
- Nothing fires before **09:00 UTC** on Monday.
- Final line is `{"event": "algo_running"}`.

### Flatten / 51008 notes

- The algo now auto-recovers from `51008` on a sell: `chase_sell` falls back to
  a **taker cross** (position-aware) to flatten a residual long — same mechanic
  as `force_liquidate`. Root condition is ~0/negative BTC balance; keep a small
  BTC buffer to avoid the taker fee.
- Manual `force_liquidate` must run with the algo **stopped** (it holds a lock),
  and with the entrypoint cleared:
  ```bash
  docker-compose -p okx_signal stop algo
  docker-compose -p okx_signal run --rm --entrypoint "" algo python tools/force_liquidate.py
  docker-compose -p okx_signal start algo
  ```

### Schedule model (why Monday early-morning was disabled)

Daily options roll at **08:00 UTC**, so the "trading day" runs 08:00→08:00.
Entries before 08:00 UTC belong to the PREVIOUS trading day and are cron'd one
calendar day later: `wd_*` pre-08:00 fire Tue–Sat, `we_*` pre-08:00 fire
Sun/Mon. Consequence with weekends OFF: **Mon 00:00–08:00 UTC is silent**
(weekend tail) and **Sat 00:00–08:00 UTC trades as a weekday** (Friday's expiry).

---

## Pre-flight checklist

- [ ] `~/okx-signal/.env` → account 2 keys; passphrase filled (verified in step 4)
- [ ] `diagnose_okx.py` showed account 2 (NOT account 1)
- [ ] `CONTAINER_NAME=okx_signal` set in the signal `.env`
- [ ] `COMPOSE_PROJECT_NAME=okx_signal` set in the signal `.env` (so the `-p` flag can't be forgotten — see Gotcha A)
- [ ] Signal file present: `/opt/tft/vsn-vol-forecaster/signals/trade_gate.json`
- [ ] `TRADE_GATE_ENABLED=true` ONLY on the signal stack
- [ ] Telegram bot/chat differs from the plain stack (or left blank)
- [ ] `INITIAL_CAPITAL_USD` set to account 2's real capital
- [ ] Started with `DRY_RUN=true`, flipped to `false` only after verifying wiring
- [ ] Host clock synced (chrony / systemd-timesyncd) — cron + gate freshness need accurate UTC
- [ ] Plain stack at `~/okx_0dte_straddle` left untouched
