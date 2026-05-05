"""
Option chain management for OKX.

Fetches BTC options, filters to 0DTE, and provides strike/instrument lookup.
OKX instrument naming: BTC-USD-YYMMDD-STRIKE-{C|P}
  e.g. BTC-USD-260418-65000-C  →  call, expiry 18-Apr-2026, strike $65k

Uses bulk get_tickers_for_underlying() for efficiency instead of per-instrument
polling.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

import config
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
        """
        expiry_str = today_expiry_instid_str()
        underlying = f"{config.BASE_COIN}-{config.QUOTE_COIN}"
        self.calls.clear()
        self.puts.clear()

        tickers = await self._exchange.get_tickers_for_underlying(underlying)
        if not tickers:
            log.warning("no_tickers_for_underlying", uly=underlying)
            return 0

        for name, ticker in tickers.items():
            # Expected: BASE-QUOTE-YYMMDD-STRIKE-{C|P}
            parts = name.split("-")
            if len(parts) != 5:
                continue
            base, quote, exp, strike_str, opt_type = parts
            if base != config.BASE_COIN or quote != config.QUOTE_COIN:
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
                 calls=len(self.calls), puts=len(self.puts), total=total)
        return total
