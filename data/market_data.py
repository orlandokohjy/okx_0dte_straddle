"""Market data fetcher for OKX — spot price and option tickers."""
from __future__ import annotations

import structlog

from core.exchange import OKXExchange
from data.option_chain import OptionChain

log = structlog.get_logger(__name__)


class MarketData:
    def __init__(self, exchange: OKXExchange, chain: OptionChain) -> None:
        self._exchange = exchange
        self._chain = chain

    async def get_spot_price(self) -> float:
        return await self._exchange.get_spot_price()

    async def get_option_bid_ask(self, instrument: str) -> tuple[float, float]:
        ticker = await self._exchange.get_ticker(instrument)
        return ticker.bid, ticker.ask
