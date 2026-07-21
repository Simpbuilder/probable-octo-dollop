"""Offline tests for permanent hooked archives, recreation, and guarded ready deletion."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from archive import ArchiveManager
from archive.recreation import ClipRecreationService
from collector.models import ArchiveConfig, ClipMetadata
from collector.storage import load_clip_metadata, save_clip_metadata, update_clip_metadata
from publisher.history import append_post_history, build_post_history_record


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def make_clip(unique_id: str, *, source_file: Path | None, ready_file: Path | None) -> ClipMetadata:
    """Create a minimal downloaded hooked-ready record for an isolated local archive test."""
    return ClipMetadata(
        unique_id=unique_id,
        source="manual",
        subreddit=None,
        source_post_id=unique_id,
        source_url=f"https://example.invalid/{unique_id}",
        title=f"Clip {unique_id}",
        author="manual",
        score=0,
        comment_count=0,
        created_at=NOW,
        media_url=None,
        local_file_path=source_file,
        download_status="downloaded" if source_file else "pending",
        processing_status="ready" if ready_file else "pending",
        added_at=NOW,
        formatted_file_path=ready_file,
        formatted_width=1080 if ready_file else None,
        formatted_height=1920 if ready_file else None,
        selected_hook="That went sideways",
        hook_text="That went sideways",
        hook_status="rendered",
        hook_source="generated",
    )


class FakeDownloader:
    """Restore one local pending file without network access."""

    def __init__(self, metadata_file: Path, source_file: Path) -> None:
        self.metadata_file = metadata_file
        self.source_file = source_file
        self.calls = 0

    def run(self, *, process_all: bool, clip_ids: frozenset[str]):
        self.calls += 1
        clip_id = next(iter(clip_ids))
        clip = load_clip_metadata(self.metadata_file, clip_id)
        assert clip is not None
        self.source_file.parent.mkdir(parents=True, exist_ok=True)
        self.source_file.write_bytes(b"source")
        update_clip_metadata(
            self.metadata_file,
            replace(clip, local_file_path=self.source_file, download_status="downloaded"),
        )
        return type("Summary", (), {"failed": 0})()


class FakeFormatter:
    """Create exactly one hooked ready output using saved hook metadata, without FFmpeg."""

    def __init__(self, metadata_file: Path, ready_directory: Path) -> None:
        self.metadata_file = metadata_file
        self.ready_directory = ready_directory
        self.calls = 0

    def run(self, *, process_all: bool, clip_ids: frozenset[str]):
        self.calls += 1
        clip_id = next(iter(clip_ids))
        clip = load_clip_metadata(self.metadata_file, clip_id)
        assert clip is not None
        output = self.ready_directory / "hooked" / f"{clip_id}-hook.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"hooked")
        update_clip_metadata(
            self.metadata_file,
            replace(clip, formatted_file_path=output, processing_status="ready"),
        )
        return type("Summary", (), {"failed": 0})()


class ArchiveManagerTests(unittest.TestCase):
    """Verify archive copy correctness and make destructive scope explicit."""

    def make_environment(self):
        """Create one clean project-like set of metadata, ready, source, and archive paths."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        metadata_file = root / "metadata" / "clips.json"
        ready_directory = root / "clips" / "ready"
        archive_directory = root / "clips" / "archive" / "hooked"
        manager = ArchiveManager(
            metadata_file,
            ready_directory,
            ArchiveConfig(archive_directory=archive_directory),
        )
        return root, metadata_file, ready_directory, archive_directory, manager

    def test_archive_copies_hooked_output_and_records_verified_metadata(self) -> None:
        """A completed hooked render has a separate hash-verified permanent copy."""
        root, metadata_file, ready_directory, archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "clip.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"source")
        ready = ready_directory / "hooked" / "clip.mp4"
        ready.parent.mkdir(parents=True)
        ready.write_bytes(b"hooked content")
        clip = make_clip("clip", source_file=source, ready_file=ready)
        save_clip_metadata(metadata_file, clip)

        result = manager.archive_formatted_clip(clip)

        updated = load_clip_metadata(metadata_file, "clip")
        self.assertTrue(result.archived)
        self.assertEqual(result.archive_path, archive_directory / ready.name)
        self.assertEqual((archive_directory / ready.name).read_bytes(), ready.read_bytes())
        self.assertEqual(updated.archive_status, "archived")
        self.assertEqual(updated.archive_path, archive_directory / ready.name)
        self.assertIsNotNone(updated.archive_hash)
        self.assertIsNone(updated.archive_error)

    def test_archive_missing_repairs_only_missing_records_and_verification_reports_loss(self) -> None:
        """Archive repair is idempotent and verification does not silently replace a missing file."""
        root, metadata_file, ready_directory, archive_directory, manager = self.make_environment()
        ready = ready_directory / "hooked" / "repair.mp4"
        ready.parent.mkdir(parents=True)
        ready.write_bytes(b"hooked content")
        clip = make_clip("repair", source_file=None, ready_file=ready)
        save_clip_metadata(metadata_file, clip)

        summary = manager.archive_missing()
        archived_path = archive_directory / ready.name
        archived_path.unlink()
        verification = manager.verify_archive()

        self.assertEqual(summary.archived, 1)
        self.assertEqual(verification.missing, 1)
        self.assertIn("archived file is missing", verification.findings[0])

    def test_changed_ready_output_creates_a_new_archive_version_without_overwriting_prior_copy(self) -> None:
        """Default archive settings preserve an earlier permanent file when the new render differs."""
        root, metadata_file, ready_directory, archive_directory, manager = self.make_environment()
        ready = ready_directory / "hooked" / "versioned.mp4"
        ready.parent.mkdir(parents=True)
        ready.write_bytes(b"first")
        clip = make_clip("versioned", source_file=None, ready_file=ready)
        save_clip_metadata(metadata_file, clip)
        manager.archive_formatted_clip(clip)
        first_archive = archive_directory / ready.name
        ready.write_bytes(b"second content")

        result = manager.archive_formatted_clip(load_clip_metadata(metadata_file, "versioned"))

        self.assertTrue(result.archived)
        self.assertNotEqual(result.archive_path, first_archive)
        self.assertEqual(first_archive.read_bytes(), b"first")
        self.assertEqual(result.archive_path.read_bytes(), b"second content")

    def test_ready_deletion_is_limited_to_ready_file_and_preserves_archive_and_hook_data(self) -> None:
        """A user deletion cannot remove a source, archive copy, hook choice, or external history."""
        root, metadata_file, ready_directory, archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "delete.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"source")
        ready = ready_directory / "hooked" / "delete.mp4"
        ready.parent.mkdir(parents=True)
        ready.write_bytes(b"hooked")
        clip = make_clip("delete", source_file=source, ready_file=ready)
        save_clip_metadata(metadata_file, clip)
        manager.archive_formatted_clip(clip)

        result = manager.delete_ready_output("delete")

        updated = load_clip_metadata(metadata_file, "delete")
        self.assertTrue(result.deleted)
        self.assertFalse(ready.exists())
        self.assertTrue(source.exists())
        self.assertTrue((archive_directory / ready.name).exists())
        self.assertEqual(updated.selected_hook, "That went sideways")
        self.assertEqual(updated.processing_status, "pending")
        self.assertIsNone(updated.formatted_file_path)
        self.assertTrue(updated.deleted_by_user)
        self.assertIsNotNone(updated.deleted_at)

    def test_plain_ready_deletion_preserves_source_and_upload_history(self) -> None:
        """The same guarded delete supports plain output without touching any remote-history file."""
        root, metadata_file, ready_directory, _archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "plain.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"source")
        ready = ready_directory / "plain" / "plain.mp4"
        ready.parent.mkdir(parents=True)
        ready.write_bytes(b"plain")
        history_file = root / "metadata" / "zernio_post_history.json"
        append_post_history(
            history_file,
            build_post_history_record(
                post_id="remote-1",
                status="published",
                account_id="account-1",
                filename=ready.name,
                public_media_url="https://media.example/plain.mp4",
                publish_mode="publish_now",
            ),
        )
        before_history = history_file.read_bytes()
        clip = make_clip("plain", source_file=source, ready_file=ready)
        save_clip_metadata(metadata_file, clip)

        result = manager.delete_ready_output("plain")

        self.assertTrue(result.deleted)
        self.assertTrue(source.is_file())
        self.assertEqual(history_file.read_bytes(), before_history)
        self.assertEqual(load_clip_metadata(metadata_file, "plain").selected_hook, "That went sideways")

    def test_invalid_ready_path_and_missing_file_leave_metadata_unchanged(self) -> None:
        """User-controlled metadata cannot escape ready directories and failed deletion is non-corrupting."""
        root, metadata_file, _ready_directory, _archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "source.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"source")
        outside = root / "outside.mp4"
        outside.write_bytes(b"outside")
        clip = make_clip("outside", source_file=source, ready_file=outside)
        save_clip_metadata(metadata_file, clip)

        result = manager.delete_ready_output("outside")

        unchanged = load_clip_metadata(metadata_file, "outside")
        self.assertFalse(result.deleted)
        self.assertIn("limited to clips/ready", result.error)
        self.assertTrue(outside.is_file())
        self.assertEqual(unchanged.formatted_file_path, outside)
        self.assertFalse(unchanged.deleted_by_user)

    def test_recreation_reuses_source_when_present_and_never_calls_downloader(self) -> None:
        """Recreation uses the existing source first, renders one item, and refreshes the archive."""
        root, metadata_file, ready_directory, archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "recreate.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"source")
        clip = make_clip("recreate", source_file=source, ready_file=None)
        save_clip_metadata(metadata_file, clip)
        downloader = FakeDownloader(metadata_file, source)
        formatter = FakeFormatter(metadata_file, ready_directory)

        result = ClipRecreationService(metadata_file, downloader, formatter, manager).recreate("recreate")

        updated = load_clip_metadata(metadata_file, "recreate")
        self.assertTrue(result.archived)
        self.assertEqual(downloader.calls, 0)
        self.assertEqual(formatter.calls, 1)
        self.assertEqual(updated.recreation_count, 1)
        self.assertTrue((archive_directory / "recreate-hook.mp4").is_file())

    def test_recreation_redownloads_only_when_source_is_missing(self) -> None:
        """The downloader is targeted to the selected clip when its original local source is absent."""
        root, metadata_file, ready_directory, _archive_directory, manager = self.make_environment()
        source = root / "clips" / "pending" / "restored.mp4"
        clip = make_clip("restored", source_file=None, ready_file=None)
        save_clip_metadata(metadata_file, clip)
        downloader = FakeDownloader(metadata_file, source)
        formatter = FakeFormatter(metadata_file, ready_directory)

        result = ClipRecreationService(metadata_file, downloader, formatter, manager).recreate("restored")

        self.assertTrue(result.archived)
        self.assertEqual(downloader.calls, 1)
        self.assertEqual(formatter.calls, 1)
