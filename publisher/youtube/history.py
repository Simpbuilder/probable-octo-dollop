"""Durable local and legacy YouTube upload history with conservative duplicate matching."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


SUCCESSFUL_YOUTUBE_STATUSES = frozenset({"uploaded", "published", "success", "completed"})


def load_youtube_history(history_file: Path) -> list[dict[str, object]]:
    """Load this project's upload history, returning no records before the first upload."""
    return _load_history_records(history_file, context="YouTube upload history")


def load_external_youtube_history(history_file: Path) -> list[dict[str, object]]:
    """Load the proven legacy project's history without changing it or requiring its presence."""
    return _load_history_records(history_file, context="external YouTube upload history")


def append_youtube_history(history_file: Path, record: Mapping[str, object]) -> None:
    """Atomically append one successful upload record unless its video ID already exists."""
    records = load_youtube_history(history_file)
    video_id = _string_value(record.get("youtube_video_id"))
    if video_id and any(_string_value(item.get("youtube_video_id")) == video_id for item in records):
        return
    records.append(dict(record))
    payload = {"schema_version": 1, "uploads": records}
    path = Path(history_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def build_youtube_history_record(
    *,
    video_file: Path,
    file_hash: str,
    video_id: str,
    title: str,
    privacy_status: str,
    channel_id: str,
) -> dict[str, object]:
    """Build the complete durable success record for future local duplicate checks."""
    return {
        "status": "uploaded",
        "local_filename": video_file.name,
        "filename_stem": video_file.stem,
        "file_hash": file_hash,
        "youtube_video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "privacy_status": privacy_status,
        "channel_id": channel_id,
    }


def history_has_duplicate(
    records: Iterable[Mapping[str, object]],
    video_file: Path,
    file_hash: str,
    *,
    youtube_video_id: str | None = None,
) -> bool:
    """Match known successful records by filename, stem, hash, or an already-stored ID."""
    expected_name = video_file.name.casefold()
    expected_stem = video_file.stem.casefold()
    expected_video_id = (youtube_video_id or "").strip()
    for record in records:
        if not _record_is_successful(record):
            continue
        record_name = _string_value(record.get("local_filename") or record.get("video_filename"))
        record_stem = _string_value(record.get("filename_stem"))
        record_hash = _string_value(record.get("file_hash"))
        record_video_id = _string_value(record.get("youtube_video_id"))
        if record_name.casefold() == expected_name or record_stem.casefold() == expected_stem:
            return True
        if file_hash and record_hash == file_hash:
            return True
        if expected_video_id and record_video_id == expected_video_id:
            return True
    return False


def known_video_ids(records: Iterable[Mapping[str, object]]) -> frozenset[str]:
    """Return successful video IDs only, so remote channel checks cannot be broadened by bad data."""
    return frozenset(
        video_id
        for record in records
        if _record_is_successful(record)
        if (video_id := _string_value(record.get("youtube_video_id")))
    )


def _load_history_records(history_file: Path, *, context: str) -> list[dict[str, object]]:
    """Read either the local schema or the legacy list schema without accepting malformed records."""
    path = Path(history_file)
    if not path.exists():
        return []
    try:
        raw_data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read {context}: {error}") from error
    records = raw_data.get("uploads") if isinstance(raw_data, dict) else raw_data
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError(f"{context.capitalize()} must contain a list of upload records.")
    return [dict(record) for record in records]


def _record_is_successful(record: Mapping[str, object]) -> bool:
    """Treat legacy records with a video ID as successful because they predate an explicit status field."""
    status = _string_value(record.get("status"))
    if status:
        return status.casefold() in SUCCESSFUL_YOUTUBE_STATUSES
    return bool(_string_value(record.get("youtube_video_id")))


def _string_value(value: object) -> str:
    """Return a stripped string field or a harmless empty value for malformed data."""
    return value.strip() if isinstance(value, str) else ""
