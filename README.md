# OKX 0DTE Pure Straddle Algo

Daily long-straddle on OKX BTC options.

- **Position**: 1 ITM call + 1 put (same strike) per `QTY_PER_LEG` BTC.
- **Schedule**: 12:00 UTC entry, 16:00 UTC close (Mon–Fri).
- **Execution**: Maker-only (post-only) with 50% bid-ask gap-narrowing
  chase, fair-value cap (mark × 1.15), 10-minute deadline.
- **Default mode**: Demo Trading (`OKX_FLAG=1`) + `DRY_RUN=true`. Set
  both to `0`/`false` in `.env` only when you are ready for live.

## Architecture

Mirrors the Derive 0DTE straddle algo. Key differences:

| Item | Derive | OKX |
|---|---|---|
| API | derive-client SDK + on-chain | python-okx (REST) |
| Settlement | USDC | BTC (coin-margined) |
| Symbol | `BTC-DDMMYYYY-STRIKE-{C,P}` | `BTC-USD-YYMMDD-STRIKE-{C,P}` |
| Contract size | 1 BTC | 0.01 BTC (`OKX_CONTRACT_SIZE_BTC`) |
| RFQ | Atomic via SDK | Stubbed — block trading not standard |

## Safety primitives (carried over from Derive/Bybit)

- Maker-only chase (post-only orders, rejected if would cross)
- 50% bid-ask gap narrowing per retry — never linear price walk
- Fair-value cap: never bid above `mark × 1.15` (or sell below `mark / 1.15`)
- Pre-entry spread gate: skip session if any leg's spread > 30%
- Startup `cancel_all_open_orders()` to clear stale orders
- Startup position reconciliation — locks entries if exchange/state disagree
- Put-first entry: if put fails, session is skipped (no naked call exposure)
- Emergency rollback: if call fails after put filled, sell the put

## Setup

### 1. Get OKX API credentials

For demo trading first:
1. Sign in to OKX → switch to **Demo Trading** mode
2. Go to https://www.okx.com/account/my-api?mode=demo
3. Create a new API key. **Save the passphrase** — you cannot retrieve it later.
4. Permissions needed: **Read** + **Trade** (no Withdraw)

### 2. Configure `.env`

```bash
cp .env.example .env
# edit .env, fill in:
#   OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional but recommended)
```

Default `.env.example` is set to:
- `OKX_FLAG=1` (demo trading)
- `DRY_RUN=true` (no orders sent)
- `QTY_PER_LEG=0.5`
- `NUM_STRADDLES_OVERRIDE=1`

### 3. First run — DRY_RUN smoke test

This connects to OKX demo, pulls the option chain, runs the pre-flight check, but does NOT place orders. Verify the Telegram messages look correct.

```bash
docker-compose up --build
```

To trigger entry immediately for testing:

```bash
ENTRY_NOW=true docker-compose up --build
```

### 4. Demo trading run (real demo orders)

Once the smoke test looks correct:

```bash
# .env: DRY_RUN=false  (keep OKX_FLAG=1)
docker-compose down
docker-compose up -d --build
```

Monitor logs:

```bash
docker-compose logs -f algo
```

### 5. Going live

When you're confident in demo behaviour for several sessions:

```bash
# .env: OKX_FLAG=0, DRY_RUN=false
docker-compose down
docker-compose up -d --build
```

## Common operations

```bash
# Check container status
docker ps | grep okx_0dte

# Stop the algo
docker-compose down

# Tail recent logs
docker-compose logs --tail 200 algo

# Manually fire an entry now
ENTRY_NOW=true docker-compose up -d --build
# IMPORTANT: edit .env to set ENTRY_NOW=false after, or the next restart
# will fire entry immediately.
```

## Files of interest

- `state/positions.json` — current open straddle (or null)
- `state/equity.json` — last known equity
- `state/trade_log.csv` — every closed trade
- `logs/algo.log` — full algo log

## Known limitations / TODOs

- RFQ (block trading) is stubbed. If your account has it, plug it in via
  `core/exchange.py::send_rfq()`.
- `ENTRY_NOW=true` does not auto-disable after firing — you must edit .env.
- No auto-circuit-breaker on consecutive failed sessions.
- No top-of-book size check (could post larger than the displayed bid).
