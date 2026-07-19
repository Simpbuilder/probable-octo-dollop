"""Shared local-path helpers for the vertical formatter."""

from __future__ import annotations

from pathlib import Path

from collector.file_utils import (
    concise_error_message,
    ensure_path_is_within_directory,
    safe_filename_stem,
)


def formatted_output_path(output_directory: Path, unique_id: str) -> Path:
    """Return the stable MP4 target for one clip without using source-file names."""
    return Path(output_directory) / f"{safe_filename_stem(unique_id)}.mp4"
