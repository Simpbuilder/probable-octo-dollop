"""Typed data models shared by the collector and metadata storage layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping


DownloadStatus = Literal["pending", "downloaded", "failed"]
ProcessingStatus = Literal["pending", "approved", "rejected", "ready", "posted"]
PipelineMode = Literal["reddit_api", "manual_urls", "both"]
InstagramPublishMode = Literal["draft", "publish_now"]
CropMode = Literal["fit"]
HookStatus = Literal["rendered", "skipped", "failed"]
HookSource = Literal["manual", "source_title", "generated"]
HookGenerationStatus = Literal["generated", "failed", "rejected"]
HorizontalAlignment = Literal["left", "center", "right"]


DEFAULT_BLOCKED_HOOK_PHRASES = (
    "what happens next",
    "wait for",
    "prepare for",
    "you won't believe",
    "you wont believe",
    "this will shock you",
    "watch until the end",
    "unexpected ending",
    "surprising",
    "shocking",
    "unbelievable",
)


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
    top_time_filter: str = "week"
    posts_to_inspect: int = 100
    allow_nsfw: bool = False

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
        if not self.top_time_filter.strip():
            raise ValueError("top_time_filter must not be empty.")
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
    pipeline_mode: PipelineMode = "reddit_api"
    downloader_config: "DownloaderConfig | None" = None
    formatter_config: "FormatterConfig | None" = None
    hook_generation_config: "HookGenerationConfig | None" = None
    instagram_config: "InstagramConfig | None" = None
    manual_urls_per_run: int = 50

    def __post_init__(self) -> None:
        """Validate source-agnostic intake safety limits."""
        if self.manual_urls_per_run <= 0:
            raise ValueError("manual_urls_per_run must be greater than zero.")

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
    media_url: str | None
    local_file_path: Path | None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    download_status: DownloadStatus = "pending"
    processing_status: ProcessingStatus = "pending"
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    download_error: str | None = None
    formatted_file_path: Path | None = None
    formatted_width: int | None = None
    formatted_height: int | None = None
    format_error: str | None = None
    hook_text: str | None = None
    hook_status: HookStatus | None = None
    hook_source: HookSource | None = None
    hook_error: str | None = None
    hook_candidates: tuple[str, ...] = ()
    selected_hook: str | None = None
    hook_generation_status: HookGenerationStatus | None = None
    hook_generation_error: str | None = None
    hook_generated_at: datetime | None = None
    hook_model: str | None = None

    def __post_init__(self) -> None:
        """Validate stable metadata before it is written to storage."""
        required_fields = {
            "unique_id": self.unique_id,
            "source": self.source,
            "source_post_id": self.source_post_id,
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
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
        if self.download_status not in {"pending", "downloaded", "failed"}:
            raise ValueError("download_status is not supported.")
        if self.processing_status not in {"pending", "approved", "rejected", "ready", "posted"}:
            raise ValueError("processing_status is not supported.")
        if self.download_error is not None and not self.download_error.strip():
            raise ValueError("download_error must be a non-empty string or null.")
        if self.formatted_width is not None and self.formatted_width <= 0:
            raise ValueError("formatted_width must be greater than zero when provided.")
        if self.formatted_height is not None and self.formatted_height <= 0:
            raise ValueError("formatted_height must be greater than zero when provided.")
        if self.format_error is not None and not self.format_error.strip():
            raise ValueError("format_error must be a non-empty string or null.")
        if self.hook_text is not None and not self.hook_text.strip():
            raise ValueError("hook_text must be a non-empty string or null.")
        if self.hook_status is not None and self.hook_status not in {"rendered", "skipped", "failed"}:
            raise ValueError("hook_status is not supported.")
        if self.hook_source is not None and self.hook_source not in {
            "manual",
            "source_title",
            "generated",
        }:
            raise ValueError("hook_source is not supported.")
        if self.hook_error is not None and not self.hook_error.strip():
            raise ValueError("hook_error must be a non-empty string or null.")
        if not isinstance(self.hook_candidates, tuple):
            raise ValueError("hook_candidates must be an immutable tuple.")
        if self.hook_candidates and len(self.hook_candidates) != 3:
            raise ValueError("hook_candidates must contain exactly three entries or be empty.")
        normalized_candidates = [_normalized_hook_candidate(candidate) for candidate in self.hook_candidates]
        if any(not candidate for candidate in normalized_candidates):
            raise ValueError("hook_candidates must not contain empty strings.")
        if len(set(normalized_candidates)) != len(normalized_candidates):
            raise ValueError("hook_candidates must be distinct.")
        if self.selected_hook is not None and not self.selected_hook.strip():
            raise ValueError("selected_hook must be a non-empty string or null.")
        if self.hook_generation_status is not None and self.hook_generation_status not in {
            "generated",
            "failed",
            "rejected",
        }:
            raise ValueError("hook_generation_status is not supported.")
        if self.hook_generation_error is not None and not self.hook_generation_error.strip():
            raise ValueError("hook_generation_error must be a non-empty string or null.")
        if self.hook_generated_at is not None and self.hook_generated_at.tzinfo is None:
            raise ValueError("hook_generated_at must include timezone information when provided.")
        if self.hook_model is not None and not self.hook_model.strip():
            raise ValueError("hook_model must be a non-empty string or null.")

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
            "download_error": self.download_error,
            "formatted_file_path": (
                str(self.formatted_file_path) if self.formatted_file_path else None
            ),
            "formatted_width": self.formatted_width,
            "formatted_height": self.formatted_height,
            "format_error": self.format_error,
            "hook_text": self.hook_text,
            "hook_status": self.hook_status,
            "hook_source": self.hook_source,
            "hook_error": self.hook_error,
            "hook_candidates": list(self.hook_candidates),
            "selected_hook": self.selected_hook,
            "hook_generation_status": self.hook_generation_status,
            "hook_generation_error": self.hook_generation_error,
            "hook_generated_at": (
                self.hook_generated_at.isoformat() if self.hook_generated_at else None
            ),
            "hook_model": self.hook_model,
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
            media_url=_optional_string(data, "media_url"),
            local_file_path=_optional_path(data, "local_file_path"),
            duration_seconds=_optional_float(data, "duration_seconds"),
            width=_optional_int(data, "width"),
            height=_optional_int(data, "height"),
            download_status=_required_string(data, "download_status"),  # type: ignore[arg-type]
            processing_status=_required_string(data, "processing_status"),  # type: ignore[arg-type]
            download_error=_optional_string(data, "download_error"),
            added_at=_required_datetime(data, "added_at"),
            formatted_file_path=_optional_path(data, "formatted_file_path"),
            formatted_width=_optional_int(data, "formatted_width"),
            formatted_height=_optional_int(data, "formatted_height"),
            format_error=_optional_string(data, "format_error"),
            hook_text=_optional_string(data, "hook_text"),
            hook_status=_optional_string(data, "hook_status"),  # type: ignore[arg-type]
            hook_source=_optional_string(data, "hook_source"),  # type: ignore[arg-type]
            hook_error=_optional_string(data, "hook_error"),
            hook_candidates=_optional_string_tuple(data, "hook_candidates"),
            selected_hook=_optional_string(data, "selected_hook"),
            hook_generation_status=_optional_string(
                data, "hook_generation_status"
            ),  # type: ignore[arg-type]
            hook_generation_error=_optional_string(data, "hook_generation_error"),
            hook_generated_at=_optional_datetime(data, "hook_generated_at"),
            hook_model=_optional_string(data, "hook_model"),
        )


@dataclass(frozen=True, slots=True)
class DownloaderConfig:
    """Validated local settings for downloading pending media files."""

    directory: Path
    preferred_format: str = "mp4"
    maximum_duration_seconds: int | None = 90
    maximum_file_size_bytes: int | None = 104_857_600
    retries: int = 2
    timeout_seconds: int = 30
    overwrite: bool = False
    downloads_per_run: int = 50
    enabled: bool = False

    def __post_init__(self) -> None:
        """Validate settings before a downloader can make filesystem changes."""
        if not self.preferred_format.strip():
            raise ValueError("preferred_format must not be empty.")
        if self.maximum_duration_seconds is not None and self.maximum_duration_seconds <= 0:
            raise ValueError("maximum_duration_seconds must be greater than zero or null.")
        if self.maximum_file_size_bytes is not None and self.maximum_file_size_bytes <= 0:
            raise ValueError("maximum_file_size_bytes must be greater than zero or null.")
        if self.retries < 0:
            raise ValueError("retries must be zero or greater.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        if self.downloads_per_run <= 0:
            raise ValueError("downloads_per_run must be greater than zero.")


@dataclass(frozen=True, slots=True)
class HookGenerationConfig:
    """Validated OpenAI hook-generation settings kept separate from rendering style."""

    enabled: bool = False
    model: str = "gpt-4o-mini"
    maximum_characters: int = 60
    maximum_clips_per_run: int = 50
    automatic_selection: bool = False
    blocked_phrases: tuple[str, ...] = DEFAULT_BLOCKED_HOOK_PHRASES

    def __post_init__(self) -> None:
        """Reject unsafe queue limits and incomplete model settings before API use."""
        if not self.model.strip():
            raise ValueError("hook generation model must not be empty.")
        if self.maximum_characters <= 0:
            raise ValueError("hook generation maximum_characters must be greater than zero.")
        if self.maximum_clips_per_run <= 0:
            raise ValueError("hook generation maximum_clips_per_run must be greater than zero.")
        if not isinstance(self.blocked_phrases, tuple):
            raise ValueError("hook generation blocked_phrases must be an immutable tuple.")
        if any(not isinstance(phrase, str) for phrase in self.blocked_phrases):
            raise ValueError("hook generation blocked_phrases must contain strings.")
        normalized_phrases = [phrase.strip().casefold() for phrase in self.blocked_phrases]
        if not normalized_phrases:
            raise ValueError("hook generation blocked_phrases must not be empty.")
        if any(not phrase for phrase in normalized_phrases):
            raise ValueError("hook generation blocked_phrases must not contain empty values.")
        if len(set(normalized_phrases)) != len(normalized_phrases):
            raise ValueError("hook generation blocked_phrases must be distinct.")


@dataclass(frozen=True, slots=True)
class InstagramConfig:
    """Validated settings for explicit Zernio-backed Instagram Reel uploads."""

    enabled: bool = False
    platform: str = "instagram"
    account_id: str | None = None
    account_username: str | None = None
    source_directory: Path = Path("clips/ready/hooked")
    publish_mode: InstagramPublishMode = "draft"
    default_caption: str = ""
    maximum_uploads_per_run: int = 1
    delete_after_upload: bool = False
    move_after_upload: bool = False
    posted_directory: Path = Path("clips/posted")
    duplicate_check_enabled: bool = True
    delay_between_posts_enabled: bool = True
    delay_between_posts_seconds: int = 30
    maximum_delay_seconds: int = 300

    def __post_init__(self) -> None:
        """Validate upload safety settings before local files can be sent remotely."""
        if self.platform != "instagram":
            raise ValueError("instagram platform must be 'instagram'.")
        if self.account_id is not None and not self.account_id.strip():
            raise ValueError("instagram account_id must be a non-empty string or null.")
        if self.account_username is not None and not self.account_username.strip():
            raise ValueError("instagram account_username must be a non-empty string or null.")
        if self.publish_mode not in {"draft", "publish_now"}:
            raise ValueError("instagram publish_mode must be 'draft' or 'publish_now'.")
        if not self.default_caption.strip():
            raise ValueError("instagram default_caption must not be empty.")
        if self.maximum_uploads_per_run <= 0:
            raise ValueError("instagram maximum_uploads_per_run must be greater than zero.")
        if self.delete_after_upload and self.move_after_upload:
            raise ValueError("instagram cannot delete and move the same uploaded file.")
        if self.delay_between_posts_seconds < 0:
            raise ValueError("instagram delay_between_posts_seconds must be zero or greater.")
        if self.maximum_delay_seconds < 0:
            raise ValueError("instagram maximum_delay_seconds must be zero or greater.")
        if self.delay_between_posts_seconds > self.maximum_delay_seconds:
            raise ValueError(
                "instagram delay_between_posts_seconds cannot exceed maximum_delay_seconds."
            )


@dataclass(frozen=True, slots=True)
class HookConfig:
    """Validated text-style settings for optional hook overlays."""

    enabled: bool = False
    font_path: Path | None = None
    font_size: int = 72
    font_color: str = "black"
    maximum_text_width: int = 900
    maximum_lines: int = 3
    line_spacing: int = 10
    horizontal_alignment: HorizontalAlignment = "center"
    vertical_position: int = 36
    text_box_height: int = 248
    text_padding: int = 20
    fallback_to_source_title: bool = True
    minimum_font_size: int = 42
    automatic_font_shrinking: bool = True
    outline_color: str | None = None
    outline_width: int = 0
    shadow_color: str | None = None
    shadow_offset: int = 0

    def __post_init__(self) -> None:
        """Validate text bounds before a hook overlay can be rendered."""
        if self.font_size <= 0 or self.minimum_font_size <= 0:
            raise ValueError("hook font sizes must be greater than zero.")
        if self.minimum_font_size > self.font_size:
            raise ValueError("hook minimum_font_size must not exceed font_size.")
        if not self.font_color.strip():
            raise ValueError("hook font_color must not be empty.")
        if self.maximum_text_width <= 0:
            raise ValueError("hook maximum_text_width must be greater than zero.")
        if self.maximum_lines <= 0:
            raise ValueError("hook maximum_lines must be greater than zero.")
        if self.line_spacing < 0:
            raise ValueError("hook line_spacing must be zero or greater.")
        if self.horizontal_alignment not in {"left", "center", "right"}:
            raise ValueError("hook horizontal_alignment is not supported.")
        if self.vertical_position < 0 or self.text_padding < 0:
            raise ValueError("hook vertical_position and text_padding must be zero or greater.")
        if self.text_box_height <= 0:
            raise ValueError("hook text_box_height must be greater than zero.")
        if self.outline_width < 0 or self.shadow_offset < 0:
            raise ValueError("hook outline_width and shadow_offset must be zero or greater.")
        if self.outline_color is not None and not self.outline_color.strip():
            raise ValueError("hook outline_color must be a non-empty string or null.")
        if self.shadow_color is not None and not self.shadow_color.strip():
            raise ValueError("hook shadow_color must be a non-empty string or null.")
        if self.outline_width and self.outline_color is None:
            raise ValueError("hook outline_color is required when outline_width is greater than zero.")
        if self.shadow_offset and self.shadow_color is None:
            raise ValueError("hook shadow_color is required when shadow_offset is greater than zero.")
        shadow_space = self.shadow_offset if self.shadow_color is not None else 0
        if self.maximum_text_width <= (2 * self.outline_width) + shadow_space:
            raise ValueError("hook maximum_text_width leaves no room for text.")
        if self.text_box_height <= (2 * self.text_padding) + (2 * self.outline_width) + shadow_space:
            raise ValueError("hook text_box_height leaves no room for text.")


@dataclass(frozen=True, slots=True)
class FormatterConfig:
    """Validated layout and encoding settings for vertical-ready clip output."""

    output_directory: Path
    enabled: bool = False
    output_width: int = 1080
    output_height: int = 1920
    background_color: str = "white"
    horizontal_margin: int = 60
    top_text_area_height: int = 320
    bottom_margin: int = 120
    maximum_video_width: int = 960
    maximum_video_height: int = 1400
    crop_mode: CropMode = "fit"
    output_frame_rate: int = 30
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 20
    encoding_preset: str = "medium"
    overwrite: bool = False
    maximum_clips_per_run: int = 50
    hook: HookConfig = field(default_factory=HookConfig)

    def __post_init__(self) -> None:
        """Validate values before FFmpeg can create media files."""
        if self.output_width <= 0 or self.output_height <= 0:
            raise ValueError("output dimensions must be greater than zero.")
        if self.output_width % 2 or self.output_height % 2:
            raise ValueError("output dimensions must be even for yuv420p output.")
        if self.horizontal_margin < 0 or self.top_text_area_height < 0 or self.bottom_margin < 0:
            raise ValueError("margins and top_text_area_height must be zero or greater.")
        if self.maximum_video_width <= 0 or self.maximum_video_height <= 0:
            raise ValueError("maximum video dimensions must be greater than zero.")
        if self.output_width - (2 * self.horizontal_margin) <= 0:
            raise ValueError("horizontal_margin leaves no space for video.")
        if self.output_height - self.top_text_area_height - self.bottom_margin <= 0:
            raise ValueError("text area and bottom margin leave no space for video.")
        if self.crop_mode != "fit":
            raise ValueError("crop_mode must be 'fit'.")
        if self.output_frame_rate <= 0:
            raise ValueError("output_frame_rate must be greater than zero.")
        if not self.background_color.strip():
            raise ValueError("background_color must not be empty.")
        if not self.video_codec.strip() or not self.audio_codec.strip():
            raise ValueError("video_codec and audio_codec must not be empty.")
        if not 0 <= self.crf <= 51:
            raise ValueError("crf must be between 0 and 51.")
        if not self.encoding_preset.strip():
            raise ValueError("encoding_preset must not be empty.")
        if self.maximum_clips_per_run <= 0:
            raise ValueError("maximum_clips_per_run must be greater than zero.")
        if self.hook.maximum_text_width + (2 * self.hook.text_padding) > self.output_width:
            raise ValueError("hook text width and padding exceed the output width.")
        if self.hook.vertical_position + self.hook.text_box_height > self.top_text_area_height:
            raise ValueError("hook text box must fit inside the top text area.")


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


def _optional_string_tuple(data: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    """Read an optional JSON string list as immutable metadata candidates."""
    value = data.get(field_name, [])
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings or null.")
    return tuple(value)


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


def _optional_datetime(data: Mapping[str, object], field_name: str) -> datetime | None:
    """Read an optional timezone-aware ISO timestamp from stored JSON metadata."""
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp string or null.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information.")
    return parsed


def _normalized_hook_candidate(candidate: str) -> str:
    """Create a stable duplicate key without altering the stored display text."""
    if not isinstance(candidate, str):
        return ""
    return " ".join(candidate.split()).casefold()
