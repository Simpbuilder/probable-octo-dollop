"""Typed values exchanged between downloader orchestration and yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """One media download request with a safe output location."""

    source_url: str
    output_directory: Path
    filename_stem: str
    preferred_format: str
    maximum_file_size_bytes: int | None
    retries: int
    timeout_seconds: int
    overwrite: bool

    @property
    def output_template(self) -> Path:
        """Return yt-dlp's extension-preserving output template for this clip."""
        return self.output_directory / f"{self.filename_stem}.%(ext)s"


@dataclass(frozen=True, slots=True)
class MediaInspection:
    """Download-relevant media properties reported before retrieval begins."""

    duration_seconds: float | None
    width: int | None
    height: int | None
    extension: str | None
    requires_ffmpeg: bool


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """A confirmed downloaded media file and its available properties."""

    local_file_path: Path
    duration_seconds: float | None
    width: int | None
    height: int | None


@dataclass(slots=True)
class DownloadSummary:
    """Counters presented after one pass over pending metadata."""

    pending: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
