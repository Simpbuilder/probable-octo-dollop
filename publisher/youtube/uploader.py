"""Explicit batch uploader for finished hooked vertical videos on YouTube."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
import time

from collector.file_utils import ensure_path_is_within_directory
from collector.models import ClipMetadata, YoutubeConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .client import YoutubeClientError, YoutubeClientProtocol
from .history import (
    append_youtube_history,
    build_youtube_history_record,
    history_has_duplicate,
    known_video_ids,
    load_external_youtube_history,
    load_youtube_history,
)
from .models import YoutubeChannel, YoutubeUploadProgress, YoutubeUploadSummary
from .utils import build_youtube_title, normalized_tags, resolve_stored_path, sha256_file


YoutubeUploadProgressCallback = Callable[[YoutubeUploadProgress], bool | None]


def count_pending_youtube_uploads(
    *,
    history_file: Path,
    config: YoutubeConfig,
) -> int:
    """Count locally eligible files using the same local and legacy duplicate records as uploads."""
    try:
        records = [
            *load_youtube_history(history_file),
            *load_external_youtube_history(config.external_history_file),
        ]
    except ValueError:
        return 0
    if not config.source_directory.is_dir():
        return 0
    pending = 0
    for video_file in config.source_directory.iterdir():
        if not video_file.is_file() or video_file.suffix.casefold() != ".mp4":
            continue
        try:
            file_hash = sha256_file(video_file)
        except OSError:
            continue
        if not history_has_duplicate(records, video_file, file_hash):
            pending += 1
    return pending


class YoutubeUploader:
    """Upload only configured ready/hooked MP4 files without deleting originals by default."""

    def __init__(
        self,
        *,
        metadata_file: Path,
        history_file: Path,
        config: YoutubeConfig,
        client: YoutubeClientProtocol,
        logger: logging.Logger | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._metadata_file = Path(metadata_file)
        self._history_file = Path(history_file)
        self._config = config
        self._client = client
        self._logger = logger or logging.getLogger(__name__)
        self._sleep_func = sleep_func

    def run(
        self,
        *,
        process_all: bool = False,
        maximum_uploads_override: int | None = None,
        progress_callback: YoutubeUploadProgressCallback | None = None,
    ) -> YoutubeUploadSummary:
        """Upload eligible files, preserve retryability, and continue when one video fails."""
        summary = YoutubeUploadSummary()
        if not self._config.enabled:
            self._logger.error("YouTube uploads are disabled in config/youtube.json.")
            summary.failed = 1
            return summary
        try:
            auth_status = self._client.authentication_status(include_channel=True)
        except YoutubeClientError as error:
            self._logger.error("YouTube uploader could not start: %s", error)
            summary.failed = 1
            return summary
        if not auth_status.token_reusable or auth_status.channel is None:
            self._logger.error(
                "YouTube uploader could not start: %s",
                auth_status.error or "No authenticated YouTube channel is available.",
            )
            summary.failed = 1
            return summary
        try:
            local_history = load_youtube_history(self._history_file)
            external_history = load_external_youtube_history(self._config.external_history_file)
        except ValueError as error:
            self._logger.error("YouTube uploader could not load upload history: %s", error)
            summary.failed = 1
            return summary
        try:
            remote_video_ids = self._client.list_uploaded_video_ids()
        except YoutubeClientError as error:
            self._logger.warning(
                "Could not inspect remote YouTube uploads; using local history only: %s", error
            )
            remote_video_ids = frozenset()

        clips_by_path = self._load_clips_by_formatted_path()
        source_files = self._hooked_mp4_files()
        summary.found = len(source_files)
        history_records = [*local_history, *external_history]
        known_ids = known_video_ids(history_records)
        eligible_files: list[tuple[Path, ClipMetadata | None, str]] = []
        for video_file in source_files:
            try:
                file_hash = sha256_file(video_file)
            except OSError as error:
                summary.skipped += 1
                self._logger.warning("Skipping unreadable YouTube source %s: %s", video_file.name, error)
                continue
            clip = clips_by_path.get(video_file.resolve())
            if self._is_duplicate(
                history_records,
                video_file,
                file_hash,
                clip,
                known_ids,
                remote_video_ids,
            ):
                summary.duplicates += 1
                continue
            if self._config.move_after_upload and (self._config.posted_directory / video_file.name).exists():
                summary.skipped += 1
                self._logger.warning(
                    "Skipping %s because its posted destination already exists.", video_file.name
                )
                continue
            eligible_files.append((video_file, clip, file_hash))

        summary.eligible = len(eligible_files)
        limit = maximum_uploads_override or self._config.maximum_uploads_per_run
        summary.processing = len(eligible_files) if process_all else min(len(eligible_files), limit)
        summary.remaining = summary.eligible - summary.processing
        processing_files = eligible_files[: summary.processing]
        for index, (video_file, clip, file_hash) in enumerate(processing_files):
            remaining_uploads = len(processing_files) - index
            if not self._emit_progress(
                progress_callback,
                phase="uploading",
                current_file=video_file,
                summary=summary,
                remaining_uploads=remaining_uploads,
                total_uploads=len(processing_files),
            ):
                summary.stopped = True
                summary.remaining += remaining_uploads
                break
            succeeded = self._upload_one(
                video_file,
                clip,
                file_hash,
                auth_status.channel,
                summary,
            )
            remaining_uploads -= 1
            if not self._emit_progress(
                progress_callback,
                phase="uploaded" if succeeded else "failed",
                current_file=video_file,
                summary=summary,
                remaining_uploads=remaining_uploads,
                total_uploads=len(processing_files),
            ):
                summary.stopped = True
                summary.remaining += remaining_uploads
                break
            if succeeded and remaining_uploads and self._config.delay_between_uploads_seconds:
                if not self._wait_between_uploads(
                    current_file=video_file,
                    summary=summary,
                    remaining_uploads=remaining_uploads,
                    total_uploads=len(processing_files),
                    progress_callback=progress_callback,
                ):
                    summary.stopped = True
                    summary.remaining += remaining_uploads
                    break
        return summary

    def _hooked_mp4_files(self) -> list[Path]:
        """List direct MP4 files from the configured hooked-output directory only."""
        source_directory = self._config.source_directory
        if not source_directory.is_dir():
            self._logger.warning("YouTube source directory does not exist: %s", source_directory)
            return []
        files: list[Path] = []
        for path in source_directory.iterdir():
            if not path.is_file() or path.suffix.casefold() != ".mp4":
                continue
            try:
                files.append(ensure_path_is_within_directory(path, source_directory))
            except ValueError:
                self._logger.warning("Skipping YouTube source outside its configured directory: %s", path)
        return sorted(files)

    def _load_clips_by_formatted_path(self) -> dict[Path, ClipMetadata]:
        """Map ready output paths to existing metadata without rejecting untracked source files."""
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except (OSError, ValueError) as error:
            self._logger.warning("Could not load clip metadata for YouTube title lookup: %s", error)
            return {}
        result: dict[Path, ClipMetadata] = {}
        for clip in clips:
            path = resolve_stored_path(self._metadata_file, clip.formatted_file_path)
            if path is not None:
                result[path] = clip
        return result

    def _is_duplicate(
        self,
        history_records: list[dict[str, object]],
        video_file: Path,
        file_hash: str,
        clip: ClipMetadata | None,
        known_ids: frozenset[str],
        remote_video_ids: frozenset[str],
    ) -> bool:
        """Apply local filename/hash/ID history first, then available remote ID confirmation."""
        if not self._config.duplicate_check_enabled:
            return False
        stored_id = clip.youtube_video_id if clip is not None else None
        if history_has_duplicate(history_records, video_file, file_hash, youtube_video_id=stored_id):
            return True
        return bool(stored_id and stored_id in known_ids and stored_id in remote_video_ids)

    def _upload_one(
        self,
        video_file: Path,
        clip: ClipMetadata | None,
        file_hash: str,
        channel: YoutubeChannel,
        summary: YoutubeUploadSummary,
    ) -> bool:
        """Upload one video, persist success history, then record matching clip metadata."""
        title = build_youtube_title(clip, video_file, self._config.default_title_template)
        try:
            result = self._client.upload_short(
                video_file,
                title=title,
                description=self._config.default_description,
                tags=normalized_tags(self._config.tags),
                category_id=self._config.category_id,
                privacy_status=self._config.privacy_status,
                made_for_kids=False,
            )
            append_youtube_history(
                self._history_file,
                build_youtube_history_record(
                    video_file=video_file,
                    file_hash=file_hash,
                    video_id=result.video_id,
                    title=title,
                    privacy_status=self._config.privacy_status,
                    channel_id=channel.channel_id,
                ),
            )
            try:
                final_path = self._finalize_local_file(video_file)
            except OSError as error:
                self._logger.error(
                    "YouTube upload succeeded but %s could not be moved: %s", video_file.name, error
                )
                final_path = video_file.resolve()
            self._mark_matching_clip_uploaded(clip, result.video_id, result.video_url, title, final_path)
            summary.uploaded += 1
            return True
        except (YoutubeClientError, OSError, ValueError) as error:
            summary.failed += 1
            self._mark_matching_clip_failure(clip, str(error))
            self._logger.error("YouTube upload failed for %s: %s", video_file.name, error)
        except Exception as error:
            summary.failed += 1
            self._mark_matching_clip_failure(clip, f"Unexpected YouTube failure: {error}")
            self._logger.exception("Unexpected YouTube upload failure for %s", video_file.name)
        return False

    def _finalize_local_file(self, video_file: Path) -> Path:
        """Move only after durable success history when configured; otherwise preserve source media."""
        if not self._config.move_after_upload:
            return video_file.resolve()
        destination_directory = self._config.posted_directory
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / video_file.name
        shutil.move(str(video_file), str(destination))
        return destination.resolve()

    def _mark_matching_clip_uploaded(
        self,
        clip: ClipMetadata | None,
        video_id: str,
        video_url: str,
        title: str,
        final_path: Path,
    ) -> None:
        """Store YouTube identity separately from Instagram and source-processing state."""
        if clip is None:
            return
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    formatted_file_path=final_path,
                    youtube_video_id=video_id,
                    youtube_video_url=video_url,
                    youtube_upload_status="uploaded",
                    youtube_upload_error=None,
                    youtube_uploaded_at=datetime.now(timezone.utc),
                    youtube_title=title,
                    youtube_privacy_status=self._config.privacy_status,
                ),
            )
        except (OSError, ValueError, KeyError) as error:
            self._logger.error("Could not update YouTube metadata for %s: %s", clip.unique_id, error)

    def _mark_matching_clip_failure(self, clip: ClipMetadata | None, error: str) -> None:
        """Record a retryable YouTube failure without changing formatter or Instagram state."""
        if clip is None:
            return
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    youtube_upload_status="failed",
                    youtube_upload_error=error,
                ),
            )
        except (OSError, ValueError, KeyError) as update_error:
            self._logger.error(
                "Could not update failed YouTube metadata for %s: %s", clip.unique_id, update_error
            )

    def _wait_between_uploads(
        self,
        *,
        current_file: Path,
        summary: YoutubeUploadSummary,
        remaining_uploads: int,
        total_uploads: int,
        progress_callback: YoutubeUploadProgressCallback | None,
    ) -> bool:
        """Pause only after a successful upload and before another eligible item starts."""
        delay_seconds = self._config.delay_between_uploads_seconds
        self._logger.info(
            "Waiting %s seconds before the next YouTube upload after %s.",
            delay_seconds,
            current_file.name,
        )
        if progress_callback is None:
            self._sleep_func(delay_seconds)
            return True
        for remaining_seconds in range(delay_seconds, 0, -1):
            if not self._emit_progress(
                progress_callback,
                phase="waiting",
                current_file=current_file,
                summary=summary,
                remaining_uploads=remaining_uploads,
                total_uploads=total_uploads,
                delay_remaining_seconds=remaining_seconds,
            ):
                return False
            self._sleep_func(1)
        return True

    @staticmethod
    def _emit_progress(
        progress_callback: YoutubeUploadProgressCallback | None,
        *,
        phase: str,
        current_file: Path | None,
        summary: YoutubeUploadSummary,
        remaining_uploads: int,
        total_uploads: int,
        delay_remaining_seconds: int = 0,
    ) -> bool:
        """Notify presenters without coupling the uploader to Streamlit."""
        if progress_callback is None:
            return True
        update = YoutubeUploadProgress(
            phase=phase,
            current_file=current_file,
            uploaded_count=summary.uploaded,
            remaining_uploads=remaining_uploads,
            total_uploads=total_uploads,
            failed_count=summary.failed,
            delay_remaining_seconds=delay_remaining_seconds,
        )
        return progress_callback(update) is not False
