"""Shared local-path helpers for the vertical formatter."""

from __future__ import annotations

import hashlib
from pathlib import Path

from collector.file_utils import (
    concise_error_message,
    ensure_path_is_within_directory,
    safe_filename_stem,
)

PLAIN_OUTPUT_DIRECTORY_NAME = "plain"
HOOKED_OUTPUT_DIRECTORY_NAME = "hooked"


def formatted_output_path(
    output_directory: Path,
    unique_id: str,
    hook_text: str | None = None,
) -> Path:
    """Return a stable MP4 target in the plain or hooked ready-output directory."""
    stem = safe_filename_stem(unique_id)
    if hook_text:
        hook_digest = hashlib.sha256(hook_text.encode("utf-8")).hexdigest()[:12]
        stem = f"{stem}-hook-{hook_digest}"
        output_subdirectory = HOOKED_OUTPUT_DIRECTORY_NAME
    else:
        output_subdirectory = PLAIN_OUTPUT_DIRECTORY_NAME
    return Path(output_directory) / output_subdirectory / f"{stem}.mp4"
