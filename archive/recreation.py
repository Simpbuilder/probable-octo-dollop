"""Targeted hooked-video recreation using the existing downloader and formatter services."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from collector.file_utils import concise_error_message
from collector.models import ClipMetadata
from collector.storage import load_clip_metadata, update_clip_metadata
from downloader.downloader import PendingClipDownloader
from formatter.formatter import PendingClipFormatter

from .models import ArchiveResult
from .service import ArchiveManager


class ClipRecreationService:
    """Rebuild one selected hooked output without collecting or uploading anything."""

    def __init__(
        self,
        metadata_file: Path,
        downloader: PendingClipDownloader,
        formatter: PendingClipFormatter,
        archive_manager: ArchiveManager,
    ) -> None:
        """Keep dependency construction at the CLI boundary and make one-clip work explicit."""
        self._metadata_file = Path(metadata_file)
        self._downloader = downloader
        self._formatter = formatter
        self._archive_manager = archive_manager

    def recreate(self, clip_id: str, *, force: bool = False) -> ArchiveResult:
        """Re-download only when required, render one hooked output, then verify its archive copy."""
        clip = load_clip_metadata(self._metadata_file, clip_id)
        if clip is None:
            return ArchiveResult(clip_id, archived=False, error="Clip metadata was not found.")
        try:
            clip = self._ensure_local_source(clip)
            if clip is None:
                return ArchiveResult(clip_id, archived=False, error="Source media could not be restored.")
            update_clip_metadata(
                self._metadata_file,
                replace(clip, processing_status="pending", format_error=None, recreation_error=None),
            )
            summary = self._formatter.run(process_all=True, clip_ids=frozenset({clip_id}))
            current = load_clip_metadata(self._metadata_file, clip_id)
            if current is None or summary.failed or current.formatted_file_path is None:
                return self._record_failure(clip, "Hooked output could not be recreated.")
            if current.formatted_file_path.parent.name != "hooked":
                return self._record_failure(current, "Recreation requires a selected, manual, or fallback hook.")
            archive_result = self._archive_manager.archive_formatted_clip(current, explicit=True)
            if not archive_result.archived:
                return self._record_failure(current, archive_result.error or "Archive copy was not created.")
            archived_clip = load_clip_metadata(self._metadata_file, clip_id)
            if archived_clip is None:
                return self._record_failure(current, "Archive metadata could not be reloaded.")
            updated = replace(
                archived_clip,
                recreation_count=archived_clip.recreation_count + 1,
                last_recreated_at=datetime.now(timezone.utc),
                last_recreated_output_path=archived_clip.formatted_file_path,
                recreation_error=None,
            )
            update_clip_metadata(self._metadata_file, updated)
            return archive_result
        except Exception as error:
            return self._record_failure(clip, concise_error_message(error))

    def _ensure_local_source(self, clip: ClipMetadata) -> ClipMetadata | None:
        """Use the existing download only when metadata no longer points to a real local source."""
        if self._local_source_exists(clip):
            return clip
        if not clip.source_url.strip():
            self._record_failure(clip, "Clip has no source URL for a required re-download.")
            return None
        update_clip_metadata(
            self._metadata_file,
            replace(clip, download_status="pending", processing_status="pending", download_error=None),
        )
        summary = self._downloader.run(process_all=True, clip_ids=frozenset({clip.unique_id}))
        updated = load_clip_metadata(self._metadata_file, clip.unique_id)
        if summary.failed or updated is None or updated.download_status != "downloaded":
            self._record_failure(updated or clip, "Source media could not be re-downloaded.")
            return None
        return updated

    def _local_source_exists(self, clip: ClipMetadata) -> bool:
        """Resolve legacy project-relative metadata paths before deciding a re-download is needed."""
        if clip.local_file_path is None:
            return False
        candidate = Path(clip.local_file_path)
        if not candidate.is_absolute():
            candidate = self._metadata_file.parent.parent / candidate
        return candidate.is_file()

    def _record_failure(self, clip: ClipMetadata, message: str) -> ArchiveResult:
        """Keep recreation explicit and retryable without erasing hook, source, or upload history."""
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(clip, recreation_error=message, processing_status="pending"),
            )
        except (OSError, ValueError, KeyError):
            pass
        return ArchiveResult(clip.unique_id, archived=False, error=message)
