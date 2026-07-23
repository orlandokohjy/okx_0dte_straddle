"""Exit manager — triggers straddle unwind at session close."""
from __future__ import annotations

import structlog

from core.exchange import OKXExchange
from core import notifier
from core.portfolio import Portfolio
from data.market_data import MarketData
from strategy.straddle_builder import unwind_straddle

log = structlog.get_logger(__name__)


class ExitManager:
    def __init__(
        self, exchange: OKXExchange, market: MarketData, portfolio: Portfolio,
    ) -> None:
        self._exchange = exchange
        self._market = market
        self._portfolio = portfolio

    async def hard_close(
        self, session_name: str = "", session_label: str = "",
    ) -> float:
        """Unwind any open straddle. session_label (e.g.
        ``13:30-15:30 UTC``) is shown in the SESSION CLOSE telegram so
        multi-session deployments can tell which window triggered the
        close. session_name is kept for structured logging only.
        """
        # In stacked mode close only THIS session's straddle; otherwise fall
        # back to the single open straddle.
        target = (
            self._portfolio.get_open(session_name)
            if session_name else self._portfolio.open_straddle
        )
        if target is None:
            log.info("nothing_to_close",
                     session=session_name, label=session_label)
            return 0.0

        equity_before = self._portfolio.equity
        pnl = await unwind_straddle(
            self._exchange, self._market, self._portfolio,
            reason="session_close",
            session_name=session_name or None,
        )
        # close_straddle stores entry/exit prices, gross_pnl, fees on
        # the just-closed Straddle; pull it via the dedicated accessor
        # so notify_close can render the full breakdown.
        closed = self._portfolio.last_closed_straddle
        await notifier.notify_close(
            pnl, "session_close",
            session_label=session_label,
            straddle=closed,
            equity_before=equity_before,
            equity_after=self._portfolio.equity,
        )
        return pnl
