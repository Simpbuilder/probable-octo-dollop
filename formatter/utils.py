"""Shared local-path helpers for the vertical formatter."""

from __future__ import annotations

import hashlib
from pathlib import Path

from collector.file_utils import (
    concise_error_message,
    ensure_path_is_within_directory,
    safe_filename_stem,
)


def formatted_output_path(
    output_directory: Path,
    unique_id: str,
    hook_text: str | None = None,
) -> Path:
    """Return a stable MP4 target, keeping distinct hook variants from overwriting a reference."""
    stem = safe_filename_stem(unique_id)
    if hook_text:
        hook_digest = hashlib.sha256(hook_text.encode("utf-8")).hexdigest()[:12]
        stem = f"{stem}-hook-{hook_digest}"
    return Path(output_directory) / f"{stem}.mp4"
