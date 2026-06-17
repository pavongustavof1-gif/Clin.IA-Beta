# backend/logger.py
# Centralized logging configuration for ClinIA
# Replaces all print() statements across the backend

import logging
import sys


def setup_logger(name: str = 'clinia') -> logging.Logger:
    """
    Configure and return the ClinIA application logger.
    Outputs to stdout so Render captures logs correctly.
    Format: [LEVEL] [MODULE] message
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler — stdout for Render compatibility
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

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
