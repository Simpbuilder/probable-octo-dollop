"""Portable filesystem helpers shared by media pipeline stages."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")
MAX_FILENAME_STEM_LENGTH = 120


def safe_filename_stem(unique_id: str) -> str:
    """Convert a pipeline ID into a portable filename stem, including on Windows."""
    normalized = INVALID_FILENAME_CHARACTERS.sub("_", unique_id)
    normalized = WHITESPACE.sub("_", normalized).strip(" ._")
    if len(normalized) > MAX_FILENAME_STEM_LENGTH:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        prefix_length = MAX_FILENAME_STEM_LENGTH - len(digest) - 1
        normalized = f"{normalized[:prefix_length].rstrip(' .')}-{digest}"
    normalized = normalized.rstrip(" .")
    if not normalized:
        normalized = "clip"
    if normalized.upper() in WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return normalized


def ensure_path_is_within_directory(path: Path, directory: Path) -> Path:
    """Resolve and validate a file path so a stage cannot escape its output directory."""
    resolved_path = Path(path).resolve()
    resolved_directory = Path(directory).resolve()
    try:
        resolved_path.relative_to(resolved_directory)
    except ValueError as error:
        raise ValueError(f"File is outside the configured directory: {resolved_path}") from error
    return resolved_path


def concise_error_message(error: Exception | str) -> str:
    """Return a useful bounded one-line message suitable for JSON metadata storage."""
    message = " ".join(str(error).split()) or "Unknown media pipeline error."
    return message[:500]
