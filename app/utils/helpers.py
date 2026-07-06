"""Utility functions for file handling and safe naming."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def ensure_directory(path: str | Path) -> Path:
    """Create a directory if it does not exist."""

    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


MAX_FILENAME_LENGTH = 80


def safe_filename(value: str) -> str:
    """Convert a string into a filesystem-safe filename stem.

    Capped at MAX_FILENAME_LENGTH: a caller passing a long free-text string
    (e.g. a whole prompt) would otherwise produce a name that blows past
    Windows' path-length limit and fails with an opaque OSError at save time.
    """

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    value = value[:MAX_FILENAME_LENGTH].rstrip("_")
    return value or "prompt2pbi"


def utc_now_iso() -> str:
    """Return the current UTC time in ISO 8601 format."""

    return datetime.now(timezone.utc).isoformat()

