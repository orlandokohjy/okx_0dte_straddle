"""Structured logging setup using structlog.

Both native structlog events AND foreign stdlib records (e.g. httpx) are routed
through the SAME handlers via ``ProcessorFormatter``, so every message lands in
BOTH sinks:

  • stdout      → captured by ``docker logs`` (ephemeral; wiped on recreate)
  • logs/algo.log → host-mounted file (survives restarts / --force-recreate)

Historically structlog used ``PrintLoggerFactory`` which wrote events to stdout
ONLY, so trade-critical events (e.g. ``chase_sell_attempt`` with bid=/ask=)
never reached the file and were lost on every container recreate. Persisting
them to the file is what makes a stuck-position post-mortem possible.
"""
from __future__ import annotations

import logging
import os
import sys

import structlog

import config


def setup_logging() -> None:
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)

    # Shared pre-chain applied to BOTH native structlog events and foreign
    # stdlib records so everything carries the same timestamp/level context.
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # File is always rendered WITHOUT colours so no ANSI escape codes ever
    # pollute logs/algo.log. Console mirrors the production format.
    if config.LOG_JSON:
        console_renderer: object = structlog.processors.JSONRenderer()
        file_renderer: object = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)
        file_renderer = structlog.dev.ConsoleRenderer(colors=False)

    def _formatter(renderer: object) -> structlog.stdlib.ProcessorFormatter:
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(_formatter(console_renderer))

    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setFormatter(_formatter(file_renderer))

    root = logging.getLogger()
    root.handlers[:] = [stream_handler, file_handler]
    root.setLevel(level)

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
