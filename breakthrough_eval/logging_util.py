"""Logging setup for the breakthrough-eval system.

The library is quiet by default (a NullHandler is attached to the package logger
in ``__init__``). The CLI calls :func:`setup_logging` to turn on a stderr stream
(and optionally a file). Logs go to **stderr** so that stdout stays reserved for
results (mirrors the plan's codex convention: progress→stderr, answer→stdout).

Verbosity:
    0  -> WARNING   (default; only problems)
    1  -> INFO      (-v;  per-job / per-phase / per-API-call progress)
    2+ -> DEBUG     (-vv; request payload details, tool queries, audit detail)
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

LOGGER_NAME = "breakthrough_eval"

_CONSOLE_FMT = "%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s: %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s: %(message)s"


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name or LOGGER_NAME)


def _level_from_verbosity(verbosity: int) -> int:
    return {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)


def setup_logging(
    verbosity: int = 0,
    log_file: Optional[str] = None,
    level: Optional[str] = None,
) -> logging.Logger:
    """Configure the package logger. Returns it.

    ``level`` (e.g. "DEBUG") overrides ``verbosity`` when given. The file handler,
    if any, always captures DEBUG regardless of console level.
    """
    console_level = (
        getattr(logging, level.upper(), logging.INFO)
        if level
        else _level_from_verbosity(verbosity)
    )
    logger = logging.getLogger(LOGGER_NAME)
    # Logger level = the most verbose of console/file so records reach handlers.
    logger.setLevel(min(console_level, logging.DEBUG if log_file else console_level))
    logger.propagate = False

    # Reset our handlers (idempotent across repeated CLI calls / tests).
    for h in list(logger.handlers):
        if getattr(h, "_be_managed", False):
            logger.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    console._be_managed = True  # type: ignore[attr-defined]
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_FILE_FMT))
        fh._be_managed = True  # type: ignore[attr-defined]
        logger.addHandler(fh)
        logger.info("日志写入文件: %s (DEBUG 级)", log_file)

    return logger
