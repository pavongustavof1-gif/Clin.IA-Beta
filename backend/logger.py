# backend/logger.py
# Centralized logging configuration for ClinIA
# Replaces all print() statements across the backend

import logging
import os
import sys


def setup_logger(name: str = 'clinia') -> logging.Logger:
    """
    Configure and return the ClinIA application logger.
    Outputs to stdout so Render captures logs correctly.
    Format: [LEVEL] [MODULE] message

    Level is env-configurable via LOG_LEVEL (INFO/DEBUG/WARNING/ERROR),
    defaulting to INFO — never DEBUG by default. Render's stdout logs are
    retained, so DEBUG (which some call sites use for verbose diagnostics)
    must be an explicit opt-in, not the always-on default it was before
    Stage H1 (finding #11). This is a floor for verbosity, not a license
    to log PHI at any level — no log statement anywhere in this codebase
    should emit raw transcript or patient content, regardless of level.
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    level_name = os.environ.get('LOG_LEVEL', 'INFO').strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        # Invalid LOG_LEVEL value — fail safe to INFO rather than crash
        # or silently fall through to Python's WARNING root default.
        level = logging.INFO

    logger.setLevel(level)

    # Console handler — stdout for Render compatibility
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        fmt='[%(levelname)s] [%(module)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Prevent propagation to root logger to avoid duplicate output
    logger.propagate = False

    return logger


# Module-level logger instance — import this in all backend files
logger = setup_logger('clinia')
