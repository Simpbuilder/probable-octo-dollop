"""Simple JSON storage helpers for collected clip metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .models import ClipMetadata


METADATA_SCHEMA_VERSION = 1


class DuplicateClipError(ValueError):
    """Raised when metadata already represents the same source post or clip ID."""


def save_clip_metadata(metadata_file: Path, clip: ClipMetadata) -> None:
    """Add one clip record to the JSON metadata store.

    The write is performed through a sibling temporary file before replacing the
    store, keeping the persisted JSON valid if an interrupted write occurs.
    """
    metadata_file = Path(metadata_file)
    if clip_exists(metadata_file, clip):
        raise DuplicateClipError(
            "A clip with this unique ID or source and source post ID already exists."
        )

    clips = load_all_clip_metadata(metadata_file)
    clips.append(clip)
    _write_clip_metadata(metadata_file, clips)


def load_all_clip_metadata(metadata_file: Path) -> list[ClipMetadata]:
    """Load every clip record from a JSON metadata store, or return an empty list."""
    metadata_file = Path(metadata_file)
    if not metadata_file.exists():
        return []

    try:
        raw_data = json.loads(metadata_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Metadata file contains invalid JSON: {metadata_file}") from error

    if not isinstance(raw_data, dict):
        raise ValueError("Metadata file must contain a JSON object.")
    if raw_data.get("schema_version") != METADATA_SCHEMA_VERSION:
        raise ValueError(f"Unsupported metadata schema in {metadata_file}.")
    raw_clips = raw_data.get("clips")
    if not isinstance(raw_clips, list):
        raise ValueError("Metadata file field 'clips' must be a list.")

    clips: list[ClipMetadata] = []
    for raw_clip in raw_clips:
        if not isinstance(raw_clip, Mapping):
            raise ValueError("Each metadata record must be a JSON object.")
        clips.append(ClipMetadata.from_dict(raw_clip))
    return clips


def load_clip_metadata(metadata_file: Path, unique_id: str) -> ClipMetadata | None:
    """Load one clip record by its pipeline unique ID."""
    return next(
        (clip for clip in load_all_clip_metadata(metadata_file) if clip.unique_id == unique_id),
        None,
    )


def clip_exists(metadata_file: Path, clip: ClipMetadata) -> bool:
    """Return whether a clip ID or source post is already represented in storage."""
    return any(
        existing.unique_id == clip.unique_id
        or (
            existing.source == clip.source
            and existing.source_post_id == clip.source_post_id
        )
        for existing in load_all_clip_metadata(metadata_file)
    )


def _write_clip_metadata(metadata_file: Path, clips: list[ClipMetadata]) -> None:
    """Persist all records using a schema-versioned, atomic JSON document."""
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "clips": [clip.to_dict() for clip in clips],
    }
    temporary_file = metadata_file.with_suffix(f"{metadata_file.suffix}.tmp")
    temporary_file.write_text(
        json.dumps(document, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(metadata_file)
