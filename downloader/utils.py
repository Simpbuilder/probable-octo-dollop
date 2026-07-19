"""Filesystem and executable helpers for safe local media downloads."""

from __future__ import annotations

import hashlib
import re
import shutil
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
PARTIAL_SUFFIXES = frozenset({".part", ".ytdl"})
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


def is_ffmpeg_available() -> bool:
    """Return whether an FFmpeg executable can be found through the current PATH."""
    return shutil.which("ffmpeg") is not None


def find_completed_media_file(
    directory: Path,
    filename_stem: str,
    preferred_format: str,
) -> Path | None:
    """Find an exact completed output file while excluding yt-dlp partial artifacts."""
    directory = Path(directory)
    if not directory.is_dir():
        return None

    preferred_candidate = directory / f"{filename_stem}.{preferred_format.lstrip('.')}"
    if preferred_candidate.is_file():
        return preferred_candidate.resolve()

    candidates = [
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file()
        and candidate.stem == filename_stem
        and candidate.suffix.lower() not in PARTIAL_SUFFIXES
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda candidate: candidate.suffix.lower())[0].resolve()


def ensure_path_is_within_directory(path: Path, directory: Path) -> Path:
    """Resolve and validate a media path so a client cannot escape the output folder."""
    resolved_path = Path(path).resolve()
    resolved_directory = Path(directory).resolve()
    try:
        resolved_path.relative_to(resolved_directory)
    except ValueError as error:
        raise ValueError(f"Downloaded file is outside the configured directory: {resolved_path}") from error
    return resolved_path


def concise_error_message(error: Exception | str) -> str:
    """Store a useful, bounded one-line error message in JSON metadata."""
    message = " ".join(str(error).split()) or "Unknown download error."
    return message[:500]
