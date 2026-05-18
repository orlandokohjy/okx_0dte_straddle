"""
Option chain management for OKX.

Fetches BTC options, filters to 0DTE, and provides strike/instrument lookup.
OKX instrument naming depends on the active family (see ``core.family``):
    CM:  BTC-USD-{YYMMDD}-{STRIKE}-{C|P}
    UM:  BTC-USD_UM-{YYMMDD}-{STRIKE}-{C|P}

Both formats split into 5 dash-separated tokens (the ``_UM`` is part of
the second token, not a delimiter), so the parser here is family-agnostic.

Uses bulk get_tickers_for_underlying() for efficiency instead of per-instrument
polling.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

import config
from core import family
from core.exchange import OKXExchange
from utils.time_utils import today_expiry_instid_str

log = structlog.get_logger(__name__)


@dataclass
class OptionInfo:
    symbol: str
    strike: float
    option_type: str   # "C" or "P"
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0


class OptionChain:
    """Maintains the 0DTE option chain for BTC."""

    def __init__(self, exchange: OKXExchange) -> None:
        self._exchange = exchange
        self.calls: list[OptionInfo] = []
        self.puts: list[OptionInfo] = []

    async def refresh(self) -> int:
        """
        Fetch all BTC 0DTE option tickers in a single bulk call.
        Returns the total number of 0DTE instruments found.

        OKX shares ``uly=BTC-USD`` between CM and UM. We always query
        with the family-specific ``instFamily`` (BTC-USD for CM,
        BTC-USD_UM for UM) so OKX filters server-side. The instId
        prefix check is kept as a belt-and-suspenders client-side
        guard in case OKX ever changes that semantic.
        """
        expiry_str = today_expiry_instid_str()
        underlying = family.underlying()
        inst_family = family.instfamily()
        expected_quote = family.quote_token()
        self.calls.clear()
        self.puts.clear()

        tickers = await self._exchange.get_tickers_for_underlying(
            underlying, inst_family=inst_family,
        )
        if not tickers:
            log.warning("no_tickers_for_underlying", uly=underlying,
                        inst_family=inst_family,
                        family=family.label())
            return 0

        for name, ticker in tickers.items():
            # Expected: BASE-QUOTE-YYMMDD-STRIKE-{C|P}
            parts = name.split("-")
            if len(parts) != 5:
                continue
            base, quote, exp, strike_str, opt_type = parts
            if base != config.BASE_COIN or quote != expected_quote:
                continue
            if exp != expiry_str:
                continue

            try:
                strike = float(strike_str)
            except ValueError:
                continue

            info = OptionInfo(
                symbol=name,
                strike=strike,
                option_type=opt_type,
                bid=ticker.bid,
                ask=ticker.ask,
                mark=ticker.mark,
            )

            if opt_type == "C":
                self.calls.append(info)
            elif opt_type == "P":
                self.puts.append(info)

        self.calls.sort(key=lambda x: x.strike)
        self.puts.sort(key=lambda x: x.strike)

        total = len(self.calls) + len(self.puts)
        log.info("chain_refreshed", expiry=expiry_str,
                 family=family.label(), uly=underlying,
                 inst_family=inst_family,
                 calls=len(self.calls), puts=len(self.puts), total=total)
        return total
