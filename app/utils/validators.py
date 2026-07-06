"""Input validation helpers."""

from __future__ import annotations

from pathlib import Path


def validate_prompt(prompt: str) -> str:
    """Validate and normalize a user prompt."""

    normalized = prompt.strip()
    if len(normalized) < 3:
        raise ValueError("Prompt must be at least 3 characters long.")
    return normalized


def validate_file_exists(file_path: str | Path) -> Path:
    """Ensure the file exists before processing."""

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise ValueError(f"Expected a file path, got: {path}")
    return path


def is_allowed_extension(file_path: str | Path, allowed_extensions: list[str]) -> bool:
    """Check if a file has an allowed extension."""

    path = Path(file_path)
    return path.suffix.lower() in {ext.lower() for ext in allowed_extensions}

