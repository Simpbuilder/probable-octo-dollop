"""Tests for local JSON metadata persistence and duplicate detection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.models import ClipMetadata
from collector.storage import (
    DuplicateClipError,
    clip_exists,
    load_clip_metadata,
    save_clip_metadata,
    update_clip_metadata,
)


def make_clip(unique_id: str = "reddit-abc123") -> ClipMetadata:
    """Create deterministic metadata for storage tests."""
    return ClipMetadata(
        unique_id=unique_id,
        source="reddit",
        subreddit="funny",
        source_post_id="abc123",
        source_url="https://www.reddit.com/r/funny/comments/abc123",
        title="A test clip",
        author="test_author",
        score=42,
        comment_count=7,
        created_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        media_url="https://example.invalid/abc123.mp4",
        local_file_path=Path("clips/pending/reddit-abc123.mp4"),
    )


class MetadataStorageTests(unittest.TestCase):
    """Verify JSON metadata can round-trip and reject duplicate source posts."""

    def test_save_and_load_metadata(self) -> None:
        """A saved record preserves its serialized metadata when loaded again."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            clip = make_clip()

            save_clip_metadata(metadata_file, clip)
            loaded_clip = load_clip_metadata(metadata_file, clip.unique_id)

            self.assertIsNotNone(loaded_clip)
            self.assertEqual(loaded_clip.to_dict(), clip.to_dict())

    def test_loads_metadata_written_before_optional_pipeline_fields_were_added(self) -> None:
        """Older metadata remains readable after downloader and formatter extensions."""
        clip_data = make_clip().to_dict()
        clip_data.pop("download_error")
        clip_data.pop("formatted_file_path")
        clip_data.pop("formatted_width")
        clip_data.pop("formatted_height")
        clip_data.pop("format_error")

        loaded_clip = ClipMetadata.from_dict(clip_data)

        self.assertIsNone(loaded_clip.download_error)
        self.assertIsNone(loaded_clip.formatted_file_path)
        self.assertIsNone(loaded_clip.format_error)

    def test_detects_duplicate_source_post(self) -> None:
        """A changed pipeline ID cannot duplicate a source post already stored."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            original_clip = make_clip()
            duplicate_clip = make_clip(unique_id="reddit-different-id")
            save_clip_metadata(metadata_file, original_clip)

            self.assertTrue(clip_exists(metadata_file, duplicate_clip))
            with self.assertRaises(DuplicateClipError):
                save_clip_metadata(metadata_file, duplicate_clip)

    def test_updates_existing_metadata_record(self) -> None:
        """A downloader can atomically replace one stored record after a download."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            clip = make_clip()
            updated_clip = replace(
                clip,
                local_file_path=Path("clips/pending/reddit-abc123.mp4"),
                download_status="downloaded",
            )
            save_clip_metadata(metadata_file, clip)

            update_clip_metadata(metadata_file, updated_clip)

            loaded_clip = load_clip_metadata(metadata_file, clip.unique_id)
            self.assertIsNotNone(loaded_clip)
            self.assertEqual(loaded_clip.download_status, "downloaded")
