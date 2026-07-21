"""File-safe archive, verification, and ready-output deletion operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil

from collector.file_utils import concise_error_message, ensure_path_is_within_directory, safe_filename_stem
from collector.models import ArchiveConfig, ClipMetadata
from collector.storage import load_all_clip_metadata, load_clip_metadata, update_clip_metadata

from .models import ArchiveResult, ArchiveSummary, ArchiveVerification, ReadyDeletionResult


class ArchiveManager:
    """Maintain an append-only local copy of hooked renders without touching uploads."""

    def __init__(self, metadata_file: Path, ready_directory: Path, config: ArchiveConfig) -> None:
        """Constrain every archive and deletion path to known project directories."""
        self._metadata_file = Path(metadata_file)
        self._ready_directory = Path(ready_directory).resolve()
        self._hooked_directory = (self._ready_directory / "hooked").resolve()
        self._plain_directory = (self._ready_directory / "plain").resolve()
        self._config = config
        self._archive_directory = Path(config.archive_directory).resolve()

    def archive_formatted_clip(self, clip: ClipMetadata, *, explicit: bool = False) -> ArchiveResult:
        """Copy one hooked ready output and record a verifiable, non-fatal archive outcome."""
        if not self._config.enabled or (not explicit and not self._config.copy_on_success):
            return ArchiveResult(clip.unique_id, archived=False, skipped=True)
        source = self._hooked_output_path(clip)
        if source is None:
            return ArchiveResult(clip.unique_id, archived=False, skipped=True)
        try:
            destination = self._archive_destination(clip, source)
            self._archive_directory.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not self._config.overwrite_existing:
                if not self._same_file_content(source, destination):
                    destination = self._versioned_destination(destination)
                    shutil.copy2(source, destination)
                    self._verify_pair(source, destination)
                else:
                    self._verify_pair(source, destination)
            else:
                shutil.copy2(source, destination)
                self._verify_pair(source, destination)
            archive_hash = self._hash_file(destination) if self._config.archive_hash_enabled else None
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    archive_status="archived",
                    archive_path=destination,
                    archive_created_at=datetime.now(timezone.utc),
                    archive_hash=archive_hash,
                    archive_error=None,
                ),
            )
            return ArchiveResult(clip.unique_id, archived=True, archive_path=destination)
        except (OSError, ValueError, KeyError) as error:
            message = concise_error_message(error)
            self._record_archive_error(clip, message)
            return ArchiveResult(clip.unique_id, archived=False, error=message)

    def archive_missing(self) -> ArchiveSummary:
        """Archive every current hooked output that lacks a confirmed archive record."""
        summary = ArchiveSummary()
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except (OSError, ValueError):
            return ArchiveSummary(failed=1)
        for clip in clips:
            if self._hooked_output_path(clip) is None:
                continue
            if clip.archive_status == "archived" and self._archive_path_exists(clip):
                continue
            summary = replace(summary, eligible=summary.eligible + 1)
            result = self.archive_formatted_clip(clip, explicit=True)
            if result.archived:
                summary = replace(summary, archived=summary.archived + 1)
            elif result.skipped:
                summary = replace(summary, skipped=summary.skipped + 1)
            else:
                summary = replace(summary, failed=summary.failed + 1)
        return summary

    def verify_archive(self) -> ArchiveVerification:
        """Check metadata-backed archive files and report untracked local archive media."""
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except (OSError, ValueError) as error:
            return ArchiveVerification(findings=(f"Metadata could not be read: {error}",))
        checked = verified = missing = mismatched = 0
        findings: list[str] = []
        recorded_paths: set[Path] = set()
        for clip in clips:
            if clip.archive_path is None:
                continue
            checked += 1
            try:
                archive_path = ensure_path_is_within_directory(clip.archive_path, self._archive_directory)
                recorded_paths.add(archive_path)
                if not archive_path.is_file():
                    missing += 1
                    findings.append(f"{clip.unique_id}: archived file is missing")
                    continue
                if clip.archive_hash and self._hash_file(archive_path) != clip.archive_hash:
                    mismatched += 1
                    findings.append(f"{clip.unique_id}: archived file hash does not match metadata")
                    continue
                verified += 1
            except (OSError, ValueError) as error:
                mismatched += 1
                findings.append(f"{clip.unique_id}: {concise_error_message(error)}")
        local_files = self._media_files(self._archive_directory)
        untracked = [path for path in local_files if path not in recorded_paths]
        findings.extend(f"Untracked archive file: {path.name}" for path in untracked)
        return ArchiveVerification(
            checked=checked,
            verified=verified,
            missing=missing,
            mismatched=mismatched,
            untracked_files=len(untracked),
            findings=tuple(findings),
        )

    def delete_ready_output(self, clip_id: str) -> ReadyDeletionResult:
        """Delete only a tracked ready/plain or ready/hooked output; archive/source remain intact."""
        try:
            clip = load_clip_metadata(self._metadata_file, clip_id)
            if clip is None:
                return ReadyDeletionResult(clip_id, deleted=False, error="Clip metadata was not found.")
            if clip.formatted_file_path is None:
                return ReadyDeletionResult(clip_id, deleted=False, error="Clip has no ready output to delete.")
            path = self._ready_output_path(clip.formatted_file_path)
            if not path.is_file():
                return ReadyDeletionResult(clip_id, deleted=False, path=path, error="Ready output is missing.")
            path.unlink()
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    formatted_file_path=None,
                    formatted_width=None,
                    formatted_height=None,
                    format_error=None,
                    processing_status="pending",
                    deleted_at=datetime.now(timezone.utc),
                    deleted_by_user=True,
                ),
            )
            return ReadyDeletionResult(clip_id, deleted=True, path=path)
        except (OSError, ValueError, KeyError) as error:
            return ReadyDeletionResult(clip_id, deleted=False, error=concise_error_message(error))

    def _hooked_output_path(self, clip: ClipMetadata) -> Path | None:
        """Return a real hooked output only; plain outputs never enter the permanent archive."""
        if clip.formatted_file_path is None:
            return None
        try:
            path = ensure_path_is_within_directory(clip.formatted_file_path, self._hooked_directory)
        except ValueError:
            return None
        return path if path.is_file() else None

    def _ready_output_path(self, path: Path) -> Path:
        """Validate a ready output against the two explicit render directories."""
        for directory in (self._plain_directory, self._hooked_directory):
            try:
                return ensure_path_is_within_directory(path, directory)
            except ValueError:
                continue
        raise ValueError("Ready-file deletion is limited to clips/ready/plain and clips/ready/hooked.")

    def _archive_destination(self, clip: ClipMetadata, source: Path) -> Path:
        """Keep source names when safe, otherwise use a stable pipeline identifier stem."""
        filename = source.name if self._config.preserve_original_filename else f"{safe_filename_stem(clip.unique_id)}.mp4"
        return ensure_path_is_within_directory(self._archive_directory / filename, self._archive_directory)

    def _archive_path_exists(self, clip: ClipMetadata) -> bool:
        """Return whether the stored archive claim still points to a safe existing file."""
        if clip.archive_path is None:
            return False
        try:
            return ensure_path_is_within_directory(clip.archive_path, self._archive_directory).is_file()
        except ValueError:
            return False

    def _record_archive_error(self, clip: ClipMetadata, message: str) -> None:
        """Persist archive failures while deliberately leaving formatting success untouched."""
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(clip, archive_status="failed", archive_error=message),
            )
        except (OSError, ValueError, KeyError):
            return

    def _verify_pair(self, source: Path, destination: Path) -> None:
        """Require same size and, when configured, same SHA-256 before metadata says archived."""
        if source.stat().st_size != destination.stat().st_size:
            raise OSError("Archive copy size does not match the formatted output.")
        if self._config.verify_copy and self._hash_file(source) != self._hash_file(destination):
            raise OSError("Archive copy hash does not match the formatted output.")

    def _same_file_content(self, source: Path, destination: Path) -> bool:
        """Compare without overwriting an existing permanent archive version by default."""
        if source.stat().st_size != destination.stat().st_size:
            return False
        return not self._config.verify_copy or self._hash_file(source) == self._hash_file(destination)

    @staticmethod
    def _versioned_destination(destination: Path) -> Path:
        """Return the next stable local version name without overwriting a prior archive copy."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        candidate = destination.with_name(f"{destination.stem}-{timestamp}{destination.suffix}")
        counter = 2
        while candidate.exists():
            candidate = destination.with_name(
                f"{destination.stem}-{timestamp}-{counter}{destination.suffix}"
            )
            counter += 1
        return candidate

    @staticmethod
    def _hash_file(path: Path) -> str:
        """Calculate a bounded-memory SHA-256 digest for local archive verification."""
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _media_files(directory: Path) -> tuple[Path, ...]:
        """List archive media directly below its approved directory without following escapes."""
        if not directory.is_dir():
            return ()
        return tuple(
            path.resolve()
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in {".mp4", ".mov", ".m4v", ".webm"}
        )
