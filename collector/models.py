"""Typed data models shared by the collector and metadata storage layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping


DownloadStatus = Literal["pending", "downloaded", "failed"]
ProcessingStatus = Literal["pending", "approved", "rejected", "ready", "posted"]


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Collection rules for one potential media source."""

    name: str
    enabled: bool
    subreddits: tuple[str, ...] = ()
    minimum_score: int = 0
    maximum_clip_length_seconds: int = 90
    maximum_post_age_days: int = 14
    sorting_mode: str = "hot"
    posts_to_inspect: int = 100

    def __post_init__(self) -> None:
        """Validate source settings that are independent of a source client."""
        if not self.name.strip():
            raise ValueError("Source name must not be empty.")
        if self.minimum_score < 0:
            raise ValueError("minimum_score must be zero or greater.")
        if self.maximum_clip_length_seconds <= 0:
            raise ValueError("maximum_clip_length_seconds must be greater than zero.")
        if self.maximum_post_age_days <= 0:
            raise ValueError("maximum_post_age_days must be greater than zero.")
        if not self.sorting_mode.strip():
            raise ValueError("sorting_mode must not be empty.")
        if self.posts_to_inspect <= 0:
            raise ValueError("posts_to_inspect must be greater than zero.")
        if any(not subreddit.strip() for subreddit in self.subreddits):
            raise ValueError("subreddits must not contain empty names.")


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Validated configuration and resolved local paths for the collector."""

    source_configs: Mapping[str, SourceConfig]
    output_folders: Mapping[str, Path]
    metadata_file: Path

    @property
    def enabled_sources(self) -> tuple[str, ...]:
        """Return configured source names that are enabled for collection."""
        return tuple(
            name for name, source_config in self.source_configs.items() if source_config.enabled
        )

    def output_path(self, name: str) -> Path:
        """Return a configured output directory by workflow status name."""
        try:
            return self.output_folders[name]
        except KeyError as error:
            raise KeyError(f"Unknown output folder: {name}") from error


@dataclass(frozen=True, slots=True)
class ClipMetadata:
    """Source and local-state metadata for one collected clip candidate."""

    unique_id: str
    source: str
    subreddit: str | None
    source_post_id: str
    source_url: str
    title: str
    author: str
    score: int
    comment_count: int
    created_at: datetime
    media_url: str
    local_file_path: Path | None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    download_status: DownloadStatus = "pending"
    processing_status: ProcessingStatus = "pending"
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate stable metadata before it is written to storage."""
        required_fields = {
            "unique_id": self.unique_id,
            "source": self.source,
            "source_post_id": self.source_post_id,
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
            "media_url": self.media_url,
        }
        empty_fields = [name for name, value in required_fields.items() if not value.strip()]
        if empty_fields:
            raise ValueError(f"Clip metadata fields must not be empty: {', '.join(empty_fields)}")
        if self.score < 0 or self.comment_count < 0:
            raise ValueError("score and comment_count must be zero or greater.")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero when provided.")
        if self.width is not None and self.width <= 0:
            raise ValueError("width must be greater than zero when provided.")
        if self.height is not None and self.height <= 0:
            raise ValueError("height must be greater than zero when provided.")
        if self.created_at.tzinfo is None or self.added_at.tzinfo is None:
            raise ValueError("created_at and added_at must include timezone information.")

    def to_dict(self) -> dict[str, object]:
        """Serialize the record to JSON-compatible primitives."""
        return {
            "unique_id": self.unique_id,
            "source": self.source,
            "subreddit": self.subreddit,
            "source_post_id": self.source_post_id,
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
            "score": self.score,
            "comment_count": self.comment_count,
            "created_at": self.created_at.isoformat(),
            "media_url": self.media_url,
            "local_file_path": str(self.local_file_path) if self.local_file_path else None,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "download_status": self.download_status,
            "processing_status": self.processing_status,
            "added_at": self.added_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ClipMetadata":
        """Deserialize a metadata record previously produced by :meth:`to_dict`."""
        return cls(
            unique_id=_required_string(data, "unique_id"),
            source=_required_string(data, "source"),
            subreddit=_optional_string(data, "subreddit"),
            source_post_id=_required_string(data, "source_post_id"),
            source_url=_required_string(data, "source_url"),
            title=_required_string(data, "title"),
            author=_required_string(data, "author"),
            score=_required_int(data, "score"),
            comment_count=_required_int(data, "comment_count"),
            created_at=_required_datetime(data, "created_at"),
            media_url=_required_string(data, "media_url"),
            local_file_path=_optional_path(data, "local_file_path"),
            duration_seconds=_optional_float(data, "duration_seconds"),
            width=_optional_int(data, "width"),
            height=_optional_int(data, "height"),
            download_status=_required_string(data, "download_status"),  # type: ignore[arg-type]
            processing_status=_required_string(data, "processing_status"),  # type: ignore[arg-type]
            added_at=_required_datetime(data, "added_at"),
        )


def _required_string(data: Mapping[str, object], field_name: str) -> str:
    """Return a required string field or raise a clear data-format error."""
    value = data.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    return value


def _optional_string(data: Mapping[str, object], field_name: str) -> str | None:
    """Return an optional string field or raise a clear data-format error."""
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null.")
    return value


def _required_int(data: Mapping[str, object], field_name: str) -> int:
    """Return a required integer field without accepting booleans."""
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer.")
    return value


def _optional_int(data: Mapping[str, object], field_name: str) -> int | None:
    """Return an optional integer field without accepting booleans."""
    value = data.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer or null.")
    return value


def _optional_float(data: Mapping[str, object], field_name: str) -> float | None:
    """Return an optional numeric field as a float without accepting booleans."""
    value = data.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number or null.")
    return float(value)


def _optional_path(data: Mapping[str, object], field_name: str) -> Path | None:
    """Return an optional path field using :class:`pathlib.Path`."""
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a path string or null.")
    return Path(value)


def _required_datetime(data: Mapping[str, object], field_name: str) -> datetime:
    """Return a timezone-aware ISO 8601 datetime field."""
    value = _required_string(data, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information.")
    return parsed
