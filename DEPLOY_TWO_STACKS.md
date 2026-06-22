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

## Pre-flight checklist

- [ ] `~/okx-signal/.env` → account 2 keys; passphrase filled (verified in step 4)
- [ ] `diagnose_okx.py` showed account 2 (NOT account 1)
- [ ] `CONTAINER_NAME=okx_signal` set in the signal `.env`
- [ ] Signal file present: `/opt/tft/vsn-vol-forecaster/signals/trade_gate.json`
- [ ] `TRADE_GATE_ENABLED=true` ONLY on the signal stack
- [ ] Telegram bot/chat differs from the plain stack (or left blank)
- [ ] `INITIAL_CAPITAL_USD` set to account 2's real capital
- [ ] Started with `DRY_RUN=true`, flipped to `false` only after verifying wiring
- [ ] Host clock synced (chrony / systemd-timesyncd) — cron + gate freshness need accurate UTC
- [ ] Plain stack at `~/okx_0dte_straddle` left untouched
