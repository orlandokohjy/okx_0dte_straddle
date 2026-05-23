# AGENTS.md — OKX 0DTE Straddle Algo

**READ THIS FILE BEFORE TOUCHING ANYTHING.** It encodes the operating
conventions of the live system so we don't keep repeating mistakes that
have already been resolved.

---

## Production environment (DO NOT GUESS)

- **VPS host**: `jiayi@188.166.214.51` (Ubuntu)
- **Repo path on VPS**: `~/okx_0dte_straddle`
- **Container orchestrator**: legacy `docker-compose` v1 (HYPHENATED).
  V2 (`docker compose` with a space) is **NOT** installed. Every command
  uses the hyphenated form.
- **Service name**: `algo` (the key under `services:` in
  `docker-compose.yml`). There is no `okx-straddle` service.
- **Container name**: `okx_0dte_straddle` (used by `docker logs` /
  `docker exec` if calling docker directly — but prefer `docker-compose`).

### Canonical deploy & ops commands

```bash
# Routine code-only deploy (Python source changed, deps unchanged)
docker-compose build algo                         # NO --no-cache
docker-compose up -d --force-recreate algo
docker-compose logs -f --tail=200 algo

# Apply env-only changes (no rebuild)
docker-compose restart algo

# Status / logs
docker-compose ps
docker-compose logs --tail 200 algo

# Stop / start
docker-compose down
docker-compose up -d
```

`--no-cache` is **only** used when `requirements.txt` or `Dockerfile`
change, or when the operator explicitly wants a fresh base-image pull.
Never default to `--no-cache` — it adds 2-3 minutes of pip reinstall
for nothing on a code-only deploy.

### Force-liquidate / panic flatten

The Dockerfile has `ENTRYPOINT ["python", "main.py"]` — this means
**any `docker-compose run` or `docker-compose exec` command you pass
gets appended as ARGS to main.py**, NOT executed as a separate command.
You MUST clear the entrypoint with `--entrypoint ""` to run anything
other than the algo itself.

**The lock file `state/algo.pid` is held by pid=1 inside the running
algo container. The force_liquidate tool refuses to run while the
lock is held to prevent races on order management.**

Two correct recipes:

```bash
# RECIPE A — when the algo container is RUNNING (preferred for ops)
# Use exec, since main.py is already pid 1 inside the container and
# we just shell into it. exec does NOT use the entrypoint.
docker-compose exec algo python tools/force_liquidate.py
# This will REFUSE with LOCK HELD — that's correct. Then either:
#   - stop the algo first (Recipe B), OR
#   - pass --force ONLY if you are CERTAIN no chase is in flight

# RECIPE B — algo stopped, run via ephemeral container
docker-compose stop algo
docker-compose run --rm --entrypoint "" algo \
    python tools/force_liquidate.py
# After it completes:
docker-compose start algo
```

WRONG (will silently boot a second copy of main.py):

```bash
docker-compose run --rm algo python tools/force_liquidate.py
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
# This becomes: python main.py python tools/force_liquidate.py
# main.py ignores the extra args and starts the full algo. The
# force_liquidate script never runs. You end up with TWO algo
# copies (the original + the ephemeral) and the orphan is untouched.
```

The same `--entrypoint ""` rule applies to ANY one-shot diagnostic:

```bash
# Health check, position check, whatever — always clear entrypoint
docker-compose run --rm --entrypoint "" algo \
    python -c "import config; print([s.name for s in config.SESSIONS])"
```

### Inspect live session config inside the container

```bash
docker-compose exec algo python -c "
import config
days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
for s in config.SESSIONS:
    d=', '.join(days[i] for i in sorted(s.weekdays))
    print(f'{s.name:10s} entry {s.entry_utc} UTC days: {d:30s} enabled={s.enabled}')
"
```

---

## Family / units (NEVER CONFUSE)

- **Default family**: `CM` (BTC-USD inverse, coin-margined). Set in
  `.env` via `OPTION_FAMILY=CM`. UM (linear) exists in code but is not
  what we run in production.
- **Premium quote unit**: BTC per BTC of notional (CM). Internally,
  `Straddle.{call,put}.entry_price` is BTC-equivalent. Wire
  prices to OKX are also BTC. **Never multiply by spot twice.**
- **Contract size**: 0.01 BTC per OKX contract (overridable via
  `OKX_CONTRACT_SIZE_BTC`). `qty_per_leg=0.5 BTC` = 50 contracts.
  This caused a critical bug in `tools/force_liquidate.py` previously —
  see commit `f463f05`.

---

## Tick sizes — CM has SILENT TIERS

OKX's `/api/v5/public/instruments` reports `tickSz=0.0001` uniformly,
but the matching engine enforces a tiered tick by premium tier:

| Premium (BTC) | Real tick |
|---|---|
| `< 0.005`  | 0.0001 |
| `0.005-0.05` | **0.0005** |
| `≥ 0.05`   | 0.005  |

Always use `core.family.effective_tick_for_price()` and
`core.family.round_price_to_tick()` when computing chase prices. Never
hardcode `0.0001` again. Pre-tier-tick fix (commit `e320375`) caused a
137-attempt 711-second chase thrash on 2026-05-21 utc_0900 close.

---

## Maker-only invariant

`chase_buy` and `chase_sell` MUST submit with `post_only=True`. OKX
rejects with `sCode=51120` if the order would cross — this is the
hard safety net. The chase loop:

1. Reads live bid/ask
2. Computes new candidate price
3. **Floors to effective tick** for buys (direction="down"),
   **ceils** for sells (direction="up")
4. If new price is within half a tick of the resting order,
   **keep-alive** — does not cancel/re-fire (preserves queue priority)
5. Otherwise cancels the resting order, re-submits at the new price

If any future change removes `post_only=True` or the keep-alive guard,
revert immediately.

---

## Schedule architecture

Sessions are hardcoded in `config.SESSIONS`. `.env` only sets sizing
and enabled flags. **Day-of-week is NEVER set in `.env`.**

| Name      | Entry UTC | Close UTC | Days (weekday()) | Default sizing |
|---        |---        |---        |---               |---             |
| utc_0900  | 09:00     | 09:30     | Mon-Fri (0-4)    | pct_equity=10% |
| utc_1330  | 13:30     | 15:30     | Mon-Fri (0-4)    | pct_equity=25% |
| utc_2330  | 23:30     | 00:00 (+1d)| Mon-Fri (0-4)   | pct_equity=10% |
| utc_0100  | 01:00     | 02:00     | Tue-Sat (1-5)    | pct_equity=50% |
| utc_1430  | 14:30     | 15:30     | **Sat,Sun (5,6)** | fixed_btc=0.5 |
| utc_2230  | 22:30     | 00:00 (+1d)| **Sat,Sun (5,6)** | fixed_btc=0.5 |

`utc_0100` is Tue-Sat because its entry is at 01:00 UTC — that
corresponds to the previous Mon-Fri trading day. Sessions whose
`close_utc < entry_utc` automatically have `crosses_midnight=True`
(close on the next calendar day, with `close_weekdays` shifted by 1).

### Reporting cadence

| Report | Trigger | Coverage |
|---|---|---|
| Daily | After last close of each trading day | Per-session breakdown for that day |
| Weekly | After utc_0100 Sat close (Sat 02:00 UTC) | Mon-Fri only |
| Weekend recap | After utc_2230 Sun close (Mon 00:00 UTC) | Sat-Sun only |

The "last close of the day" is determined dynamically by
`Algo._is_last_close_for_weekday(session_name, weekday)` in `main.py`.
For Sun trading_day this is `utc_2230` (Sat close). For Mon trading_day
it's also `utc_2230` (Sun close). Tue-Sat use `utc_0100`.

---

## .env conventions

- Per-session keys use the pattern `<NAME_UPPER>_<KEY>`:
  `UTC_1430_SIZING`, `UTC_1430_PCT_EQUITY`, `UTC_1430_QTY_PER_LEG`,
  `UTC_1430_ENABLED`.
- `SIZING` is one of `pct_equity` or `fixed_btc`.
- `ENABLED=false` disables a session without code changes.
  `ENABLED=true` re-enables.
- `MAX_QTY_PER_LEG_BTC=0` disables the safety cap entirely.
  Anything > 0 caps `qty_per_leg` at that BTC value (applies to
  `pct_equity` sizing only — `fixed_btc` never trips the cap).
- `OPTION_ENTRY_CHASE_DEADLINE_MIN=25.0` is the production setting.
  The validator at startup ensures this fits inside every enabled
  session's window.
- `ENTRY_NOW=<session_name>` triggers a one-shot manual entry. Auto-
  disabled in-place after firing — see `_disable_entry_now_in_env_file`.
- Always back up `.env` before mutating: `cp .env .env.backup-$(date +%Y%m%d-%H%M%S)`.

---

## Things you've gotten wrong before — DO NOT REPEAT

1. **Service name**: it's `algo`, not `okx-straddle`.
2. **Compose binary**: `docker-compose` (hyphenated), not `docker compose`.
3. **`--no-cache`**: only on dep/Dockerfile changes; never on routine deploys.
4. **Tick rounding**: use `family.round_price_to_tick`, not raw `round(px/0.0001)*0.0001`.
5. **Session weekdays**: hardcoded in `config.py`, not configurable via `.env`.
6. **Last-close trigger**: dynamic per-weekday via `_is_last_close_for_weekday`.
   There is no single `LAST_CLOSE_SESSION_NAME` constant any more.
7. **Telegram message length**: `core.notifier.send` chunks at 4000 chars
   (HTML-aware). Don't add a separate truncation in callers.
8. **Force-liquidate units**: contracts vs BTC mismatch caused a critical
   bug — see `f463f05`. The tool now uses contracts as authoritative
   and converts via `family.contract_size_btc()`.
9. **Daily-report daemon**: there is NO separate cron. Reports are
   chained from session-close handlers in `main.py`. Adding a separate
   APScheduler job for reports would create duplicates.
10. **Telegram notifier**: HTML mode (`<b>`, `<i>`). Markdown will not
    render and the bot will reject it.

## Dockerfile ENTRYPOINT trap (added 2026-05-23)

**The Dockerfile uses `ENTRYPOINT ["python", "main.py"]` — exec form.**
That means `docker-compose run --rm algo <whatever>` ALWAYS runs
main.py and treats your `<whatever>` as ignored args. To run any
other command, you MUST clear the entrypoint:

```bash
docker-compose run --rm --entrypoint "" algo python tools/whatever.py
```

This trap has burned us once (utc_1430 orphan close, 2026-05-23 15:35
UTC). Symptom: a second main.py boot sequence appears in the output
when you expected a script's output. If that happens, IMMEDIATELY
`docker stop` the phantom container before retrying.

## NEVER invent class / method / function names

Before writing ANY one-shot diagnostic command (e.g. `docker-compose exec
algo python -c "..."`), GREP the actual code to confirm names. Common
landmines that have already burned us:

- The exchange wrapper class is **`OKXExchange`**, not `Exchange`.
- It MUST be `.connect()`-ed (sync) before any async method call —
  the constructor only sets fields, it does not initialize SDK clients.
- The async loop entrypoint is `asyncio.run(coro())`, and the OKX SDK
  is sync — every call goes through `self._call(fn, **kwargs)` which
  uses `asyncio.to_thread`.
- If you need to inspect live state, prefer reading existing logs
  (`docker-compose logs --tail=200 algo`) over running ad-hoc python.

When you must run python, the ONLY safe pattern is:

```bash
# Good — uses real class names + connect()
docker-compose exec algo python -c "
import asyncio
from core.exchange import OKXExchange
async def go():
    ex = OKXExchange()
    ex.connect()
    print(await ex.get_spot_price())
asyncio.run(go())
"
```

Always grep before writing the import line. ALWAYS.

## Transient API failures are normal, not deploy bugs

`httpx.RemoteProtocolError: Server disconnected` and similar
HTTP/2-edge errors during chase setup are TRANSIENT exchange-side
issues, not bugs in our code. Symptoms:

- Both legs fail within milliseconds of each other (shared HTTP/2 pool)
- Error originates in `get_option_mark_price` / `get_ticker` /
  similar read-only API calls
- `put_partial_qty: 0.0`, `call_partial_qty: 0.0` in the log
- `both_legs_failed_skipping_session` triggers cleanly

The algo handles this correctly:
- No partial position
- SESSION SKIPPED telegram fires
- Failure counter increments (3 strikes → circuit-break)
- Next cron fire is unaffected

DO NOT redeploy on a single transient API failure. The container is
fine; httpx self-heals its connection pool. Only intervene if:
1. Multiple consecutive sessions fail (sustained outage), OR
2. The container itself crashes (`docker-compose ps` shows it down), OR
3. The failure counter hits the limit (3) — at which point the algo
   has already entered safe mode.

---

## When in doubt

- Read this file first.
- Read `README.md` for end-user runbook.
- Read `core/family.py` top docstring for unit conventions.
- Read `config.py` `SESSIONS` definition for the schedule of truth.
- Don't invent service / file / command names — grep for them first.
