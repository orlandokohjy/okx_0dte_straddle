"""
OKX exchange wrapper.

Provides async helpers around the (sync) python-okx SDK for:
  - Spot index price
  - Option chain ticker fetch (bulk)
  - Account balance & open positions
  - Maker-only order placement (post_only) with reject-on-cross
  - Maker-only chase: 50% bid-ask gap narrowing, fair-value cap, deadline
  - Cancel-all stale orders (called at startup)

OKX BTC option naming: e.g.  BTC-USD-260418-65000-C
Contract size is 0.01 BTC (configurable in config.OKX_CONTRACT_SIZE_BTC).
Premiums are quoted in USD; settlement is in BTC for coin-margined options.

NOTE: RFQ / Block Trading is stubbed — see send_rfq() / send_rfq_sell().
Most retail accounts cannot use it, so leg-by-leg chase is the default.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import structlog

import config

log = structlog.get_logger(__name__)


# ─────────────────────────── Data classes ────────────────────────────

@dataclass
class Ticker:
    instrument: str
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    last: float = 0.0


# ─────────────────────────── Exchange ────────────────────────────────

class OKXExchange:
    """Thin async wrapper around python-okx sync clients."""

    def __init__(self) -> None:
        self._market = None
        self._public = None
        self._trade = None
        self._account = None
        self._connected: bool = False
        self.error_count: int = 0

    # ──────────────────── Connection ──────────────────────────────

    def connect(self) -> None:
        """Initialise OKX SDK clients."""
        try:
            from okx import MarketData, PublicData, Trade, Account
        except ImportError as exc:
            log.error("python_okx_missing",
                      hint="pip install python-okx",
                      exc_info=True)
            raise RuntimeError("python-okx not installed") from exc

        # Block Trading is optional — only import when USE_RFQ is on.
        BlockTrading = None
        if config.USE_RFQ:
            try:
                from okx import BlockTrading as _BT
                BlockTrading = _BT
            except ImportError:
                log.warning("python_okx_block_trading_missing",
                            note="upgrade python-okx to enable RFQ")

        creds_ok = bool(
            config.OKX_API_KEY and config.OKX_API_SECRET
            and config.OKX_PASSPHRASE
        )
        if not creds_ok and not config.DRY_RUN:
            raise RuntimeError(
                "Missing OKX credentials — set OKX_API_KEY, "
                "OKX_API_SECRET, OKX_PASSPHRASE in .env (or set "
                "DRY_RUN=true to smoke-test without credentials)"
            )

        flag = config.OKX_FLAG  # "0"=live, "1"=demo
        domain = config.OKX_DOMAIN
        self._market = MarketData.MarketAPI(
            flag=flag, domain=domain, debug=False,
        )
        self._public = PublicData.PublicAPI(
            flag=flag, domain=domain, debug=False,
        )
        self._trade = Trade.TradeAPI(
            config.OKX_API_KEY or "x",
            config.OKX_API_SECRET or "x",
            config.OKX_PASSPHRASE or "x",
            False, flag=flag, domain=domain, debug=False,
        )
        self._account = Account.AccountAPI(
            config.OKX_API_KEY or "x",
            config.OKX_API_SECRET or "x",
            config.OKX_PASSPHRASE or "x",
            False, flag=flag, domain=domain, debug=False,
        )
        self._block = None
        if BlockTrading is not None:
            try:
                self._block = BlockTrading.BlockTradingAPI(
                    config.OKX_API_KEY or "x",
                    config.OKX_API_SECRET or "x",
                    config.OKX_PASSPHRASE or "x",
                    False, flag=flag, domain=domain, debug=False,
                )
            except Exception:
                log.warning("block_trading_init_failed", exc_info=True)
                self._block = None
        self._connected = True
        log.info("okx_connected",
                 mode="DEMO" if flag == "1" else "LIVE",
                 domain=domain,
                 dry_run=config.DRY_RUN,
                 rfq=config.USE_RFQ and self._block is not None)

    # ──────────────────── Internal call helper ────────────────────

    async def _call(self, fn, *args, **kwargs) -> dict:
        """Run a sync SDK call in a thread; bump error_count on failure."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception:
            self.error_count += 1
            log.error("okx_call_failed", fn=fn.__name__, exc_info=True)
            raise

    @staticmethod
    def _data_or_empty(resp: Any) -> list:
        if not isinstance(resp, dict):
            return []
        if resp.get("code") not in ("0", 0, None):
            log.warning("okx_response_error", code=resp.get("code"),
                        msg=resp.get("msg"))
            return []
        return resp.get("data") or []

    @staticmethod
    def _f(d: dict, key: str, default: float = 0.0) -> float:
        v = d.get(key)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ──────────────────── Market data ─────────────────────────────

    async def get_spot_price(self) -> float:
        """Return BTC index price (USD)."""
        resp = await self._call(
            self._market.get_index_tickers,
            instId=f"{config.BASE_COIN}-{config.QUOTE_COIN}",
        )
        rows = self._data_or_empty(resp)
        if not rows:
            log.warning("spot_price_empty")
            return 0.0
        return self._f(rows[0], "idxPx")

    async def get_ticker(self, instrument: str) -> Ticker:
        """Single-instrument bid/ask/last."""
        resp = await self._call(self._market.get_ticker, instId=instrument)
        rows = self._data_or_empty(resp)
        if not rows:
            return Ticker(instrument=instrument)
        r = rows[0]
        return Ticker(
            instrument=instrument,
            bid=self._f(r, "bidPx"),
            ask=self._f(r, "askPx"),
            last=self._f(r, "last"),
            mark=self._f(r, "last"),  # OKX ticker has no mark; use last
        )

    async def get_tickers_for_underlying(
        self, underlying: str,
    ) -> dict[str, Ticker]:
        """Bulk fetch all option tickers for an underlying (e.g. BTC-USD)."""
        resp = await self._call(
            self._market.get_tickers,
            instType="OPTION", uly=underlying,
        )
        rows = self._data_or_empty(resp)
        out: dict[str, Ticker] = {}
        for r in rows:
            inst = r.get("instId")
            if not inst:
                continue
            out[inst] = Ticker(
                instrument=inst,
                bid=self._f(r, "bidPx"),
                ask=self._f(r, "askPx"),
                last=self._f(r, "last"),
                mark=self._f(r, "last"),
            )
        return out

    async def get_option_mark_price(self, instrument: str) -> float:
        """Fetch OKX option mark price (separate endpoint from ticker)."""
        resp = await self._call(
            self._public.get_mark_price,
            instType="OPTION", instId=instrument,
        )
        rows = self._data_or_empty(resp)
        if not rows:
            return 0.0
        return self._f(rows[0], "markPx")

    # ──────────────────── Account / positions ─────────────────────

    async def get_account_equity(self, ccy: str = "USDT") -> float:
        """Return total trading-account equity in `ccy`."""
        resp = await self._call(self._account.get_account_balance, ccy=ccy)
        rows = self._data_or_empty(resp)
        if not rows:
            return 0.0
        details = rows[0].get("details") or []
        for d in details:
            if d.get("ccy") == ccy:
                return self._f(d, "eq")
        return self._f(rows[0], "totalEq")

    async def list_open_positions(self) -> list[dict]:
        """List all option positions for BTC-USD.

        python-okx >=0.4.1 dropped the `uly=` parameter from
        `Account.get_positions`. We now fetch all OPTION positions and
        filter to BASE_COIN-QUOTE_COIN in code.
        """
        resp = await self._call(
            self._account.get_positions,
            instType="OPTION",
        )
        rows = self._data_or_empty(resp)
        family_prefix = f"{config.BASE_COIN}-{config.QUOTE_COIN}-"
        out = []
        for r in rows:
            pos = self._f(r, "pos")
            if pos == 0:
                continue
            inst = r.get("instId", "")
            if not inst.startswith(family_prefix):
                continue
            out.append({
                "instrument_name": inst,
                "amount": pos,
                "average_price": self._f(r, "avgPx"),
                "mark_price": self._f(r, "markPx"),
                "unrealized_pnl": self._f(r, "upl"),
            })
        return out

    async def get_option_position(self, instrument: str) -> float:
        """Return signed quantity (in contracts) of a single option position."""
        resp = await self._call(
            self._account.get_positions,
            instType="OPTION", instId=instrument,
        )
        rows = self._data_or_empty(resp)
        for r in rows:
            if r.get("instId") == instrument:
                return self._f(r, "pos")
        return 0.0

    # ──────────────────── Order management ────────────────────────

    async def list_open_orders(self) -> list[dict]:
        """List all currently open option orders."""
        resp = await self._call(
            self._trade.get_order_list,
            instType="OPTION",
        )
        rows = self._data_or_empty(resp)
        return rows

    async def cancel_all_open_orders(self) -> int:
        """Cancel every resting option order. Returns count cancelled."""
        orders = await self.list_open_orders()
        if not orders:
            return 0

        cancelled = 0
        for o in orders:
            inst = o.get("instId")
            oid = o.get("ordId")
            if not inst or not oid:
                continue
            try:
                resp = await self._call(
                    self._trade.cancel_order, instId=inst, ordId=oid,
                )
                if resp.get("code") in ("0", 0):
                    cancelled += 1
                else:
                    log.warning("cancel_failed",
                                instId=inst, ordId=oid,
                                code=resp.get("code"), msg=resp.get("msg"))
            except Exception:
                log.warning("cancel_exception", instId=inst, ordId=oid,
                            exc_info=True)
        log.info("startup_orders_cancelled",
                 total=len(orders), cancelled=cancelled)
        return cancelled

    async def get_order_status(self, instrument: str, order_id: str) -> dict:
        """Fetch latest status for a single order."""
        resp = await self._call(
            self._trade.get_order,
            instId=instrument, ordId=order_id,
        )
        rows = self._data_or_empty(resp)
        return rows[0] if rows else {}

    # ──────────────────── Order placement ─────────────────────────

    def _qty_to_contracts(self, qty_btc: float) -> str:
        """Convert BTC quantity to OKX contract count (rounded)."""
        contracts = qty_btc / config.OKX_CONTRACT_SIZE_BTC
        return str(int(round(contracts)))

    async def _place_limit_order(
        self,
        instrument: str,
        side: str,   # "buy" or "sell"
        qty_btc: float,
        price: float,
        post_only: bool = True,
    ) -> dict:
        """
        Place a single limit order.

        post_only=True means the order is rejected if it would cross — this
        guarantees maker fills (no taker fees, no slippage from crossing).
        """
        if config.DRY_RUN:
            log.info("dry_run_order",
                     instrument=instrument, side=side,
                     qty_btc=qty_btc, price=price, post_only=post_only)
            return {
                "ordId": f"dry-{int(time.time() * 1000)}",
                "sCode": "0", "sMsg": "dry_run",
            }

        sz = self._qty_to_contracts(qty_btc)
        ord_type = "post_only" if post_only else "limit"

        resp = await self._call(
            self._trade.place_order,
            instId=instrument,
            tdMode="cash",          # plain options buy/sell, no margin
            side=side,
            ordType=ord_type,
            sz=sz,
            px=str(price),
        )

        rows = self._data_or_empty(resp)
        if not rows:
            return {"sCode": resp.get("code"), "sMsg": resp.get("msg")}
        r = rows[0]
        log.info("order_placed",
                 instrument=instrument, side=side, qty_btc=qty_btc,
                 sz=sz, price=price, post_only=post_only,
                 ord_id=r.get("ordId"), sCode=r.get("sCode"),
                 sMsg=r.get("sMsg"))
        return r

    async def _wait_for_fill(
        self, instrument: str, order_id: str, timeout: float,
    ) -> dict:
        """Poll order status until filled / cancelled / timeout."""
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            try:
                last = await self.get_order_status(instrument, order_id)
                state = last.get("state", "")
                if state in ("filled", "canceled", "cancelled"):
                    return last
            except Exception:
                log.warning("order_status_failed", exc_info=True)
            await asyncio.sleep(1.0)
        return last

    # ──────────────────── Maker-only chase (BUY) ──────────────────

    async def chase_buy(
        self, instrument: str, qty_btc: float, initial_bid: float,
    ) -> Optional[dict]:
        """
        Maker-only buy chase: walks toward the ask by narrowing the gap by
        OPTION_CHASE_GAP_NARROW_PCT each retry, never crossing past
        mark × OPTION_CHASE_MAX_SLIPPAGE_FACTOR. Bails on deadline.

        Returns dict with average_price + order_id on full fill, else None.
        """
        deadline = time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60
        attempt = 0
        last_price = max(0.0, initial_bid)

        while time.time() < deadline:
            attempt += 1
            ticker = await self.get_ticker(instrument)
            mark = await self.get_option_mark_price(instrument)
            if mark <= 0:
                mark = ticker.last if ticker.last > 0 else ticker.ask

            bid, ask = ticker.bid, ticker.ask
            if ask <= 0:
                log.warning("chase_no_ask",
                            instrument=instrument, attempt=attempt,
                            bid=bid, ask=ask)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # If bid is missing (empty bid side, common on demo), seed it
            # using mark so we can still place a maker bid below ask.
            effective_bid = bid if bid > 0 else max(
                mark - config.OPTION_TICK_SIZE,
                config.OPTION_TICK_SIZE,
            )

            # 50% gap-narrowing: narrow remaining gap to (ask − tick) by pct
            target_top = max(effective_bid, ask - config.OPTION_TICK_SIZE)
            new_price = last_price + (target_top - last_price) \
                * config.OPTION_CHASE_GAP_NARROW_PCT
            new_price = max(new_price, effective_bid + config.OPTION_TICK_SIZE)

            # Fair-value cap: never bid above mark × max_slippage_factor
            max_price = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            if new_price > max_price:
                log.warning("chase_buy_cap_hit",
                            instrument=instrument, new_price=new_price,
                            mark=mark, max_price=max_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            new_price = round(new_price / config.OPTION_TICK_SIZE) \
                * config.OPTION_TICK_SIZE
            last_price = new_price

            log.info("chase_buy_attempt",
                     instrument=instrument, attempt=attempt,
                     price=new_price, bid=bid, ask=ask, mark=mark)

            order = await self._place_limit_order(
                instrument, "buy", qty_btc, new_price, post_only=True,
            )
            ord_id = order.get("ordId")
            sCode = str(order.get("sCode") or "")

            # Post-only rejected (would cross) → narrow more next loop
            if sCode == "51008" or "post_only" in str(order.get("sMsg", "")).lower():
                log.info("chase_buy_post_only_rejected",
                         instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            if not ord_id or sCode not in ("0", ""):
                log.warning("chase_buy_order_rejected",
                            instrument=instrument, sCode=sCode,
                            sMsg=order.get("sMsg"))
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # Wait for fill
            status = await self._wait_for_fill(
                instrument, ord_id, config.OPTION_CHASE_INTERVAL_SEC,
            )
            state = status.get("state", "")
            if state == "filled":
                avg_px = self._f(status, "avgPx", default=new_price)
                log.info("chase_buy_filled",
                         instrument=instrument, avg=avg_px, attempt=attempt)
                return {
                    "average_price": avg_px,
                    "order_id": ord_id,
                    "avgPrice": avg_px,
                }
            # Cancel the resting order before placing the next one
            try:
                await self._call(
                    self._trade.cancel_order,
                    instId=instrument, ordId=ord_id,
                )
            except Exception:
                pass

        log.error("chase_buy_deadline_exhausted", instrument=instrument)
        return None

    # ──────────────────── Maker-only chase (SELL) ─────────────────

    async def chase_sell(
        self, instrument: str, qty_btc: float, initial_ask: float,
    ) -> Optional[dict]:
        """
        Maker-only sell chase: walks toward the bid by narrowing the gap by
        OPTION_CHASE_GAP_NARROW_PCT each retry, never below
        mark / OPTION_CHASE_MAX_SLIPPAGE_FACTOR. Bails on deadline.
        """
        deadline = time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60
        attempt = 0
        last_price = max(0.0, initial_ask)

        while time.time() < deadline:
            attempt += 1
            ticker = await self.get_ticker(instrument)
            mark = await self.get_option_mark_price(instrument)
            if mark <= 0:
                mark = ticker.last if ticker.last > 0 else ticker.bid

            bid, ask = ticker.bid, ticker.ask
            if bid <= 0 and mark <= 0:
                log.warning("chase_no_bid_or_mark",
                            instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            # If ask is missing (empty offer side), seed it using mark
            # so we can still place a maker offer above bid.
            effective_ask = ask if ask > 0 else max(
                mark + config.OPTION_TICK_SIZE,
                config.OPTION_TICK_SIZE * 2,
            )
            effective_bid = bid if bid > 0 else max(
                mark - config.OPTION_TICK_SIZE,
                config.OPTION_TICK_SIZE,
            )

            target_bot = min(effective_ask, effective_bid + config.OPTION_TICK_SIZE)
            new_price = last_price - (last_price - target_bot) \
                * config.OPTION_CHASE_GAP_NARROW_PCT
            new_price = min(new_price, effective_ask - config.OPTION_TICK_SIZE)

            min_price = mark / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            if new_price < min_price:
                log.warning("chase_sell_floor_hit",
                            instrument=instrument, new_price=new_price,
                            mark=mark, min_price=min_price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            new_price = round(new_price / config.OPTION_TICK_SIZE) \
                * config.OPTION_TICK_SIZE
            last_price = new_price

            log.info("chase_sell_attempt",
                     instrument=instrument, attempt=attempt,
                     price=new_price, bid=bid, ask=ask, mark=mark)

            order = await self._place_limit_order(
                instrument, "sell", qty_btc, new_price, post_only=True,
            )
            ord_id = order.get("ordId")
            sCode = str(order.get("sCode") or "")

            if sCode == "51008" or "post_only" in str(order.get("sMsg", "")).lower():
                log.info("chase_sell_post_only_rejected",
                         instrument=instrument, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            if not ord_id or sCode not in ("0", ""):
                log.warning("chase_sell_order_rejected",
                            instrument=instrument, sCode=sCode,
                            sMsg=order.get("sMsg"))
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            status = await self._wait_for_fill(
                instrument, ord_id, config.OPTION_CHASE_INTERVAL_SEC,
            )
            state = status.get("state", "")
            if state == "filled":
                avg_px = self._f(status, "avgPx", default=new_price)
                log.info("chase_sell_filled",
                         instrument=instrument, avg=avg_px, attempt=attempt)
                return {
                    "average_price": avg_px,
                    "order_id": ord_id,
                    "avgPrice": avg_px,
                }
            try:
                await self._call(
                    self._trade.cancel_order,
                    instId=instrument, ordId=ord_id,
                )
            except Exception:
                pass

        log.error("chase_sell_deadline_exhausted", instrument=instrument)
        return None

    # ──────────────────── RFQ (Block Trading) ─────────────────────
    #
    # OKX Block Trading API:
    #   POST /api/v5/rfq/create-rfq      — submit a multi-leg RFQ
    #   GET  /api/v5/rfq/quotes          — poll counterparty quotes
    #   POST /api/v5/rfq/execute-quote   — execute the chosen quote
    #   POST /api/v5/rfq/cancel-rfq      — cancel an open RFQ
    #
    # Requires OKX Block Trading entitlement on the account (live trading)
    # or demo flag=1 (which has limited RFQ support). We always fall back
    # to leg-by-leg chase if RFQ returns no quotes inside the window.

    async def _rfq_send_two_leg(
        self,
        call_inst: str,
        put_inst: str,
        qty_btc: float,
        direction: str,  # "buy" or "sell"
    ) -> Optional[dict]:
        """
        Submit a 2-leg RFQ, wait for quotes, execute the best, return fills.

        direction='buy'  -> we are long both legs (pay premium)
        direction='sell' -> we are short both legs (receive premium)

        Returns None if RFQ is unavailable, no quotes received, or execution
        fails — caller will fall back to leg-by-leg chase.
        """
        if not config.USE_RFQ or self._block is None:
            return None

        sz = self._qty_to_contracts(qty_btc)
        if sz <= 0:
            return None

        legs = [
            {"instId": call_inst, "tdMode": "cash", "sz": str(sz),
             "side": direction},
            {"instId": put_inst, "tdMode": "cash", "sz": str(sz),
             "side": direction},
        ]

        # 1. Create RFQ (broadcast to all eligible market makers)
        try:
            create_resp = await self._call(
                self._block.create_rfq,
                counterparties=[],  # broadcast
                anonymous=True,
                legs=legs,
            )
        except Exception:
            log.warning("rfq_create_failed", exc_info=True)
            return None

        rows = self._data_or_empty(create_resp)
        if not rows:
            log.warning("rfq_create_empty", direction=direction)
            return None

        rfq_id = rows[0].get("rfqId", "")
        if not rfq_id:
            return None

        log.info("rfq_created", id=rfq_id, direction=direction,
                 call=call_inst, put=put_inst, qty=qty_btc, sz=sz)

        # 2. Poll quotes for up to RFQ_QUOTE_WAIT_SEC
        deadline = time.time() + config.RFQ_QUOTE_WAIT_SEC
        best_quote: Optional[dict] = None
        while time.time() < deadline:
            try:
                qresp = await self._call(self._block.get_quotes,
                                         rfqId=rfq_id)
                quotes = self._data_or_empty(qresp)
            except Exception:
                quotes = []

            if quotes:
                best_quote = self._pick_best_quote(quotes, direction)
                if best_quote is not None:
                    break
            await asyncio.sleep(1.0)

        if best_quote is None:
            log.warning("rfq_no_quotes", id=rfq_id,
                        wait_sec=config.RFQ_QUOTE_WAIT_SEC)
            try:
                await self._call(self._block.cancel_rfq, rfqId=rfq_id)
            except Exception:
                pass
            return None

        quote_id = best_quote.get("quoteId", "")
        log.info("rfq_quote_picked", id=rfq_id, quote=quote_id,
                 legs=best_quote.get("legs"))

        # 3. Execute the chosen quote
        try:
            exec_resp = await self._call(
                self._block.execute_quote,
                rfqId=rfq_id, quoteId=quote_id,
            )
        except Exception:
            log.warning("rfq_execute_failed", exc_info=True)
            return None

        if exec_resp.get("code") not in ("0", 0, None):
            log.warning("rfq_execute_error",
                        code=exec_resp.get("code"),
                        msg=exec_resp.get("msg"))
            return None

        # 4. Extract fill prices per leg
        call_price = 0.0
        put_price = 0.0
        for leg in best_quote.get("legs", []) or []:
            inst = leg.get("instId", "")
            px = self._f(leg, "px")
            if inst == call_inst:
                call_price = px
            elif inst == put_inst:
                put_price = px

        if call_price <= 0 or put_price <= 0:
            log.warning("rfq_missing_leg_price",
                        call_price=call_price, put_price=put_price)
            return None

        log.info("rfq_executed", id=rfq_id, quote=quote_id,
                 call_price=call_price, put_price=put_price,
                 direction=direction)

        return {
            "rfq_id": rfq_id,
            "quote_id": quote_id,
            "call_price": call_price,
            "put_price": put_price,
        }

    @staticmethod
    def _pick_best_quote(quotes: list[dict], direction: str) -> Optional[dict]:
        """Pick the cheapest buy quote or richest sell quote across legs."""
        scored = []
        for q in quotes:
            legs = q.get("legs") or []
            total = 0.0
            ok = True
            for leg in legs:
                v = leg.get("px")
                try:
                    total += float(v)
                except (TypeError, ValueError):
                    ok = False
                    break
            if ok and total > 0:
                scored.append((total, q))
        if not scored:
            return None
        if direction == "buy":
            scored.sort(key=lambda x: x[0])  # cheapest first
        else:
            scored.sort(key=lambda x: x[0], reverse=True)  # richest first
        return scored[0][1]

    async def send_rfq(
        self, call_inst: str, put_inst: str, qty_btc: float,
    ) -> Optional[dict]:
        """Atomic RFQ buy of (long call + long put). Returns fills or None."""
        return await self._rfq_send_two_leg(call_inst, put_inst, qty_btc,
                                            "buy")

    async def send_rfq_sell(
        self, call_inst: str, put_inst: str, qty_btc: float,
    ) -> Optional[dict]:
        """Atomic RFQ sell of (short call + short put). Returns fills or None."""
        return await self._rfq_send_two_leg(call_inst, put_inst, qty_btc,
                                            "sell")
