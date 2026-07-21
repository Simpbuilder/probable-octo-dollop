"""Small immutable data contracts shared by the Streamlit view and its helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PipelineActionResult:
    """Captured output and exit status from one existing pipeline action."""

    arguments: tuple[str, ...]
    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class UrlAppendResult:
    """The outcome of adding de-duplicated valid URL lines to the local intake queue."""

    added: int
    duplicates: int
    invalid_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardCounts:
    """Small status counters rendered by the local Streamlit dashboard."""

    urls_waiting: int
    pending_metadata: int
    downloaded_clips: int
    awaiting_hook_generation: int
    awaiting_hook_review: int
    ready_hooked_videos: int
    uploaded_or_posted: int
    failed_items: int


@dataclass(frozen=True, slots=True)
class SystemAvailability:
    """Availability booleans that deliberately reveal no credential values."""

    ffmpeg: bool
    ffprobe: bool
    openai_api_key: bool
    zernio_api_key: bool


@dataclass(frozen=True, slots=True)
class PipelineProgress:
    """Read-only queue sizes for the pipeline stages shown in the local UI."""

    urls_to_import: int
    downloads_to_run: int
    hooks_to_generate: int
    hooks_to_review: int
    formats_to_run: int
    uploads_to_run: int


@dataclass(frozen=True, slots=True)
class InstagramOverview:
    """Safe local Instagram account and history summary for the UI."""

    account_username: str | None
    publish_mode: str
    fixed_caption: str
    pending_uploads: int
    history_total: int
    drafts: int
    published: int
    delay_enabled: bool = True
    delay_seconds: int = 30
    maximum_delay_seconds: int = 300
    estimated_batch_seconds: int = 0


@dataclass(frozen=True, slots=True)
class FailedItem:
    """One stored clip error suitable for a concise UI table."""

    clip_id: str
    title: str
    error: str


@dataclass(frozen=True, slots=True)
class ReadyVideo:
    """A hooked-ready local video together with metadata and upload-state details."""

    path: Path
    selected_hook: str | None
    processing_status: str
    upload_status: str


@dataclass(frozen=True, slots=True)
class UiConfigurationValues:
    """Editable settings intentionally limited to common queue and publishing controls."""

    downloads_per_run: int
    hook_generations_per_run: int
    formats_per_run: int
    uploads_per_run: int
    instagram_publish_mode: str
    instagram_caption: str
    instagram_account_id: str | None
    automatic_hook_selection: bool
    instagram_delay_enabled: bool = True
    instagram_delay_seconds: int = 30
    instagram_maximum_delay_seconds: int = 300
