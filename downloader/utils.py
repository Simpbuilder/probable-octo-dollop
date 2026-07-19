"""Filesystem and executable helpers for safe local media downloads."""

from __future__ import annotations

import shutil
from pathlib import Path

from collector.file_utils import (
    concise_error_message,
    ensure_path_is_within_directory,
    safe_filename_stem,
)

PARTIAL_SUFFIXES = frozenset({".part", ".ytdl"})


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
