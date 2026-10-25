"""The archive job writes its own lines into the terminal's log, so support can read both in one file."""

import logging
from pathlib import Path


def job_logger(log_path) -> logging.Logger:
    """A logger that appends to the terminal's log file (one per terminal, reused across runs)."""
    log_path = Path(log_path).resolve()
    logger = logging.getLogger(f"dbarchive.{log_path.stem}")
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "baseFilename", None) == str(log_path) for h in logger.handlers):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s dbarchive %(message)s"))
        logger.addHandler(handler)
    return logger
