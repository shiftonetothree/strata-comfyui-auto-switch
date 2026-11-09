"""Logging setup: a rotating file plus a console when attached to a terminal."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from .config import RelayConfig

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(config: RelayConfig) -> logging.Logger:
    logger = logging.getLogger("relay")
    logger.setLevel(getattr(logging, config.log_level, logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if sys.stderr.isatty() or os.environ.get("RELAY_CONSOLE") == "1":
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)

    return logger
