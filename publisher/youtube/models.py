"""Typed state passed between the YouTube OAuth client, uploader, and presenters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class YoutubeChannel:
    """Safe identity fields returned for the authenticated YouTube channel."""

    channel_id: str
    channel_name: str


@dataclass(frozen=True, slots=True)
class YoutubeAuthenticationStatus:
    """Read-only OAuth readiness without ever returning a token or credential content."""

    credentials_available: bool
    token_available: bool
    token_reusable: bool
    channel: YoutubeChannel | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class YoutubeUploadResult:
    """Successful YouTube video identity returned by one completed resumable upload."""

    video_id: str
    video_url: str


@dataclass(slots=True)
class YoutubeUploadSummary:
    """Counters shown after an explicit YouTube upload pass."""

    found: int = 0
    eligible: int = 0
    processing: int = 0
    remaining: int = 0
    uploaded: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class YoutubeUploadProgress:
    """An optional progress update for terminal output or the existing Streamlit worker UI."""

    phase: str
    current_file: Path | None
    uploaded_count: int
    remaining_uploads: int
    total_uploads: int
    failed_count: int = 0
    delay_remaining_seconds: int = 0
