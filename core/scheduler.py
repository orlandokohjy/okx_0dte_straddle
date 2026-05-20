"""APScheduler wrapper — registers entry+close jobs for every Session
in config.SESSIONS.

Multi-session support: each Session gets its own entry job and close
job, scheduled on its own UTC weekday filter. The handler callbacks
accept the Session as their only argument so the same callback handles
every session, looking up qty_per_leg and metadata from the Session.

Reports are NOT scheduled here. The DAILY SUMMARY, DAILY REPORT and
(on the last trading day of the week) WEEKLY REPORT are all chained
directly off the morning close handler so they arrive within seconds
of each other rather than spread across the day. See main.py
``_on_close`` for the chained-report logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import structlog

import config
from utils.time_utils import UTC

log = structlog.get_logger(__name__)


SessionHandler = Callable[[config.Session], Awaitable[None]]


class Scheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def register_session(
        self,
        on_entry: SessionHandler,
        on_close: SessionHandler,
    ) -> None:
        """Register entry+close cron jobs for every Session.

        Reports are emitted from inside ``on_close`` (right after the
        trading-day's morning close), so this method only schedules
        entries and closes.
        """
        def _wd_str(weekdays_set) -> str:
            names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            return ",".join(names[d] for d in sorted(weekdays_set))

        for session in config.SESSIONS:
            # Honour the operator panic-button: <NAME>_ENABLED=false in
            # .env removes the session from the scheduler entirely. No
            # entry cron, no close cron, nothing fires for this session.
            # The session is still discoverable via config.get_session()
            # so reports rendering historical data still find it.
            if not session.enabled:
                log.info(
                    "session_skipped_disabled",
                    name=session.name,
                    note=(
                        "session.enabled=False (env var "
                        f"{session.name.upper()}_ENABLED=false). "
                        "No entry/close cron registered."
                    ),
                )
                continue

            entry_t = session.entry_utc
            close_t = session.close_utc
            entry_days_str = _wd_str(session.weekdays)
            # Cross-midnight closes (e.g. utc_2330 entry Mon 23:30 → close
            # Tue 00:00) need a CLOSE weekday set shifted +1 day so the
            # close fires on the calendar day AFTER each entry. Same-day
            # closes have close_weekdays == weekdays automatically.
            close_days_str = _wd_str(session.close_weekdays)

            self._scheduler.add_job(
                on_entry,
                CronTrigger(
                    hour=entry_t.hour, minute=entry_t.minute,
                    day_of_week=entry_days_str, timezone=UTC,
                ),
                id=f"session_entry_{session.name}",
                name=(
                    f"Session Entry [{session.name}] "
                    f"({entry_t.hour:02d}:{entry_t.minute:02d} UTC)"
                ),
                args=[session],
                replace_existing=True,
            )

            self._scheduler.add_job(
                on_close,
                CronTrigger(
                    hour=close_t.hour, minute=close_t.minute,
                    day_of_week=close_days_str, timezone=UTC,
                ),
                id=f"session_close_{session.name}",
                name=(
                    f"Session Close [{session.name}] "
                    f"({close_t.hour:02d}:{close_t.minute:02d} UTC)"
                ),
                args=[session],
                replace_existing=True,
            )

            log.info(
                "session_scheduled",
                name=session.name,
                entry=f"{entry_t.hour:02d}:{entry_t.minute:02d} UTC",
                close=f"{close_t.hour:02d}:{close_t.minute:02d} UTC",
                sizing=session.describe_sizing(),
                fallback_qty_per_leg=session.qty_per_leg,
                entry_days=entry_days_str,
                close_days=close_days_str,
                crosses_midnight=session.crosses_midnight,
                trading_day_offset_days=session.trading_day_offset_days,
            )

        log.info(
            "all_sessions_scheduled",
            sessions=[s.name for s in config.SESSIONS],
            last_close_session=config.LAST_CLOSE_SESSION_NAME,
            reports="chained off morning close (see main._on_close)",
        )

    def start(self) -> None:
        self._scheduler.start()
        log.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def get_next_fire_times(self) -> dict[str, datetime | None]:
        return {
            job.id: job.next_run_time
            for job in self._scheduler.get_jobs()
        }
