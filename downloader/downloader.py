"""Orchestration for safely downloading pending clip metadata entries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import logging
from pathlib import Path

from collector.models import ClipMetadata, DownloaderConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .models import DownloadRequest, DownloadResult, DownloadSummary, MediaInspection
from .utils import (
    concise_error_message,
    ensure_path_is_within_directory,
    find_completed_media_file,
    is_ffmpeg_available,
    safe_filename_stem,
)
from .yt_dlp_client import UnsupportedMediaError, YtDlpClientError, YtDlpClientProtocol


class PendingClipDownloader:
    """Download eligible pending clips one at a time without stopping on failures."""

    def __init__(
        self,
        metadata_file: Path,
        config: DownloaderConfig,
        media_client: YtDlpClientProtocol,
        *,
        ffmpeg_available: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up storage, output settings, and injectable external dependencies."""
        self._metadata_file = Path(metadata_file)
        self._config = config
        self._media_client = media_client
        self._ffmpeg_available = ffmpeg_available or is_ffmpeg_available
        self._logger = logger or logging.getLogger(__name__)

    def run(self) -> DownloadSummary:
        """Download up to the configured limit of pending clips with source URLs."""
        summary = DownloadSummary()
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except Exception as error:
            summary.failed = 1
            self._logger.error("Could not load pending clip metadata: %s", error)
            return summary

        pending_clips = [clip for clip in clips if clip.download_status == "pending"]
        summary.pending = len(pending_clips)
        eligible_clips = [clip for clip in pending_clips if clip.source_url.strip()]
        for clip in eligible_clips[: self._config.downloads_per_run]:
            self._process_clip(clip, summary)
        return summary

    def _process_clip(self, clip: ClipMetadata, summary: DownloadSummary) -> None:
        """Handle one pending clip and record a recoverable state on every failure."""
        existing_metadata_file = self._existing_metadata_file(clip)
        if existing_metadata_file is not None:
            self._mark_existing_file_downloaded(clip, existing_metadata_file, summary)
            return

        filename_stem = safe_filename_stem(clip.unique_id)
        existing_target_file = find_completed_media_file(
            self._config.directory, filename_stem, self._config.preferred_format
        )
        if existing_target_file is not None and not self._config.overwrite:
            # The unique-ID filename makes this safe to reconcile after a prior
            # download completed but its metadata write was interrupted.
            self._mark_existing_file_downloaded(clip, existing_target_file, summary)
            return

        try:
            self._config.directory.mkdir(parents=True, exist_ok=True)
            request = DownloadRequest(
                source_url=clip.source_url,
                output_directory=self._config.directory,
                filename_stem=filename_stem,
                preferred_format=self._config.preferred_format,
                maximum_file_size_bytes=self._config.maximum_file_size_bytes,
                retries=self._config.retries,
                timeout_seconds=self._config.timeout_seconds,
                overwrite=self._config.overwrite,
            )
            inspection = self._media_client.inspect(request)
            if self._exceeds_maximum_duration(inspection):
                self._record_pending_issue(
                    clip,
                    "Media duration exceeds the configured maximum download duration.",
                )
                summary.skipped += 1
                return
            if inspection.requires_ffmpeg and not self._ffmpeg_available():
                self._record_pending_issue(
                    clip,
                    "FFmpeg is required to merge the selected video and audio streams but was not found on PATH.",
                )
                summary.skipped += 1
                return

            result = self._media_client.download(request, inspection)
            self._mark_downloaded(clip, result, inspection)
            summary.downloaded += 1
        except UnsupportedMediaError as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.skipped += 1
        except (YtDlpClientError, OSError, ValueError) as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.failed += 1
            self._logger.error("Download failed for %s: %s", clip.unique_id, error)
        except Exception as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.failed += 1
            self._logger.exception("Unexpected download failure for %s", clip.unique_id)

    def _existing_metadata_file(self, clip: ClipMetadata) -> Path | None:
        """Return an existing local path recorded in metadata, including legacy relative paths."""
        if clip.local_file_path is None:
            return None
        candidate = Path(clip.local_file_path)
        if not candidate.is_absolute():
            candidate = self._config.directory / candidate.name
        return candidate.resolve() if candidate.is_file() else None

    def _mark_existing_file_downloaded(
        self,
        clip: ClipMetadata,
        local_file_path: Path,
        summary: DownloadSummary,
    ) -> None:
        """Reconcile an interrupted metadata update without downloading over the file."""
        try:
            updated_clip = replace(
                clip,
                local_file_path=ensure_path_is_within_directory(
                    local_file_path, self._config.directory
                ),
                download_status="downloaded",
                download_error=None,
            )
            update_clip_metadata(self._metadata_file, updated_clip)
            summary.skipped += 1
        except (OSError, ValueError, KeyError) as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.failed += 1

    def _mark_downloaded(
        self,
        clip: ClipMetadata,
        result: DownloadResult,
        inspection: MediaInspection,
    ) -> None:
        """Persist final media location and extractor properties after a completed download."""
        local_file_path = ensure_path_is_within_directory(
            result.local_file_path, self._config.directory
        )
        if not local_file_path.is_file():
            raise YtDlpClientError("yt-dlp reported a completed download but the media file is missing.")
        updated_clip = replace(
            clip,
            local_file_path=local_file_path,
            duration_seconds=result.duration_seconds or inspection.duration_seconds,
            width=result.width or inspection.width,
            height=result.height or inspection.height,
            download_status="downloaded",
            processing_status="pending",
            download_error=None,
        )
        update_clip_metadata(self._metadata_file, updated_clip)

    def _record_pending_issue(self, clip: ClipMetadata, message: str) -> None:
        """Keep an unsuccessful clip pending and save its bounded retry message."""
        try:
            updated_clip = replace(
                clip,
                download_status="pending",
                download_error=message,
            )
            update_clip_metadata(self._metadata_file, updated_clip)
        except Exception as error:
            self._logger.error(
                "Could not store the retry message for %s: %s", clip.unique_id, error
            )

    def _exceeds_maximum_duration(self, inspection: MediaInspection) -> bool:
        """Return whether known media duration exceeds the configured safety limit."""
        return (
            self._config.maximum_duration_seconds is not None
            and inspection.duration_seconds is not None
            and inspection.duration_seconds > self._config.maximum_duration_seconds
        )
