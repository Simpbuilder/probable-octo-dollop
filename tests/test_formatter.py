"""Offline queue tests for vertical metadata updates and failure isolation."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.models import ClipMetadata, FormatterConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from formatter.ffmpeg_client import FfmpegClientError, FfmpegDependencyError
from formatter.formatter import PendingClipFormatter
from formatter.models import FormatRequest, FormatResult, InputMediaProperties


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
DEFAULT_PROPERTIES = InputMediaProperties(width=1280, height=720, has_audio=True)


class FakeFfmpegClient:
    """A local deterministic adapter that never invokes FFmpeg or the network."""

    def __init__(
        self,
        *,
        available: bool = True,
        inspections: dict[Path, InputMediaProperties | Exception] | None = None,
        format_outcomes: dict[Path, Exception] | None = None,
    ) -> None:
        """Configure isolated inspection and render outcomes for individual files."""
        self.available = available
        self.inspections = inspections or {}
        self.format_outcomes = format_outcomes or {}
        self.inspect_requests: list[Path] = []
        self.format_requests: list[FormatRequest] = []

    def ensure_available(self) -> None:
        """Raise the same focused dependency error used by the real adapter."""
        if not self.available:
            raise FfmpegDependencyError("Missing required executable(s) on PATH: ffmpeg, ffprobe.")

    def inspect(self, input_file: Path) -> InputMediaProperties:
        """Return configured local source properties or raise an inspection error."""
        self.inspect_requests.append(input_file)
        outcome = self.inspections.get(input_file, DEFAULT_PROPERTIES)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def format(self, request: FormatRequest) -> FormatResult:
        """Write a small placeholder MP4 result or raise a configured FFmpeg failure."""
        self.format_requests.append(request)
        outcome = self.format_outcomes.get(request.input_file)
        if outcome is not None:
            raise outcome
        request.output_file.parent.mkdir(parents=True, exist_ok=True)
        request.output_file.write_bytes(b"formatted media")
        return FormatResult(output_file=request.output_file)


def make_clip(unique_id: str, local_file_path: Path | None) -> ClipMetadata:
    """Create downloaded, unprocessed metadata for one temporary source file."""
    return ClipMetadata(
        unique_id=unique_id,
        source="manual",
        subreddit=None,
        source_post_id=unique_id,
        source_url=f"https://example.invalid/{unique_id}",
        title=f"Clip {unique_id}",
        author="manual_intake",
        score=0,
        comment_count=0,
        created_at=NOW,
        media_url=None,
        local_file_path=local_file_path,
        width=1280,
        height=720,
        download_status="downloaded",
        processing_status="pending",
        added_at=NOW,
    )


class PendingClipFormatterTests(unittest.TestCase):
    """Verify formatting preserves original media metadata and remains retryable."""

    def make_environment(self, clips: list[ClipMetadata], *, maximum_clips: int = 5):
        """Create metadata, source, and ready paths isolated from the repository."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        metadata_file = root / "metadata" / "clips.json"
        ready_directory = root / "clips" / "ready"
        for clip in clips:
            save_clip_metadata(metadata_file, clip)
        config = FormatterConfig(
            output_directory=ready_directory,
            maximum_clips_per_run=maximum_clips,
        )
        return metadata_file, ready_directory, config

    def make_formatter(
        self,
        metadata_file: Path,
        config: FormatterConfig,
        client: FakeFfmpegClient,
    ) -> PendingClipFormatter:
        """Build a quiet formatter whose external behavior is fully injected."""
        logger = logging.getLogger(f"test_formatter_{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return PendingClipFormatter(metadata_file, config, client, logger=logger)

    def create_source_file(self, root: Path, unique_id: str) -> Path:
        """Create a tiny local placeholder used only as an existing input path."""
        source_file = root / "clips" / "pending" / f"{unique_id}.mp4"
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes(b"downloaded media")
        return source_file

    def test_success_updates_ready_metadata_without_replacing_original_path(self) -> None:
        """A successful render records a separate 1080x1920 ready output path."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-success")
            clip = make_clip("format-success", source_file)
            metadata_file, ready_directory, config = self.make_environment([clip])
            client = FakeFfmpegClient()

            summary = self.make_formatter(metadata_file, config, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(updated_clip.processing_status, "ready")
            self.assertEqual(updated_clip.local_file_path, source_file)
            self.assertEqual(updated_clip.formatted_file_path, (ready_directory / "format-success.mp4").resolve())
            self.assertEqual((updated_clip.formatted_width, updated_clip.formatted_height), (1080, 1920))
            self.assertIsNone(updated_clip.format_error)
            self.assertTrue(updated_clip.formatted_file_path.is_file())

    def test_output_filename_uses_a_windows_safe_unique_id_stem(self) -> None:
        """Formatter output never uses source filenames or invalid path characters."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-safe-name")
            clip = make_clip("format/safe:name", source_file)
            metadata_file, ready_directory, config = self.make_environment([clip])

            summary = self.make_formatter(metadata_file, config, FakeFfmpegClient()).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(updated_clip.formatted_file_path, (ready_directory / "format_safe_name.mp4").resolve())

    def test_missing_input_file_remains_pending_for_retry(self) -> None:
        """Missing downloaded media stores an error and does not call ffprobe."""
        with TemporaryDirectory() as temporary_directory:
            missing_file = Path(temporary_directory) / "clips" / "pending" / "missing.mp4"
            clip = make_clip("format-missing", missing_file)
            metadata_file, _, config = self.make_environment([clip])
            client = FakeFfmpegClient()

            summary = self.make_formatter(metadata_file, config, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.failed, 1)
            self.assertEqual(updated_clip.processing_status, "pending")
            self.assertIn("does not exist", updated_clip.format_error)
            self.assertEqual(client.inspect_requests, [])

    def test_missing_ffmpeg_stops_before_any_clip_is_inspected(self) -> None:
        """A missing local prerequisite is reported before source processing begins."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-prerequisite")
            metadata_file, _, config = self.make_environment(
                [make_clip("format-prerequisite", source_file)]
            )
            client = FakeFfmpegClient(available=False)

            with self.assertRaises(FfmpegDependencyError):
                self.make_formatter(metadata_file, config, client).run()

            self.assertEqual(client.inspect_requests, [])

    def test_ffmpeg_failure_keeps_processing_pending(self) -> None:
        """A per-clip FFmpeg failure records a retryable formatter error."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-failure")
            metadata_file, _, config = self.make_environment([make_clip("format-failure", source_file)])
            client = FakeFfmpegClient(
                format_outcomes={source_file: FfmpegClientError("encoder failed")}
            )

            summary = self.make_formatter(metadata_file, config, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.failed, 1)
            self.assertEqual(updated_clip.processing_status, "pending")
            self.assertEqual(updated_clip.format_error, "encoder failed")

    def test_corrupt_or_unreadable_input_keeps_processing_pending(self) -> None:
        """An ffprobe-style input failure is recorded without claiming a ready output."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-corrupt")
            metadata_file, _, config = self.make_environment([make_clip("format-corrupt", source_file)])
            client = FakeFfmpegClient(
                inspections={source_file: FfmpegClientError("ffprobe failed: invalid data")}
            )

            summary = self.make_formatter(metadata_file, config, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.failed, 1)
            self.assertEqual(updated_clip.processing_status, "pending")
            self.assertIn("invalid data", updated_clip.format_error)
            self.assertEqual(client.format_requests, [])

    def test_existing_output_is_not_overwritten_and_is_reconciled(self) -> None:
        """A ready target from an interrupted metadata write is reused without FFmpeg."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-existing")
            metadata_file, ready_directory, config = self.make_environment(
                [make_clip("format-existing", source_file)]
            )
            ready_directory.mkdir(parents=True)
            output_file = ready_directory / "format-existing.mp4"
            output_file.write_bytes(b"existing ready media")
            client = FakeFfmpegClient()

            summary = self.make_formatter(metadata_file, config, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.skipped, 1)
            self.assertEqual(updated_clip.processing_status, "ready")
            self.assertEqual(client.inspect_requests, [])
            self.assertEqual(output_file.read_bytes(), b"existing ready media")

    def test_one_failed_clip_does_not_stop_later_formatting(self) -> None:
        """A bad source file does not prevent a later valid queue item from becoming ready."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_source = self.create_source_file(root, "format-first")
            second_source = self.create_source_file(root, "format-second")
            clips = [make_clip("format-first", first_source), make_clip("format-second", second_source)]
            metadata_file, _, config = self.make_environment(clips)
            client = FakeFfmpegClient(
                inspections={first_source: FfmpegClientError("corrupt input")}
            )

            summary = self.make_formatter(metadata_file, config, client).run()
            clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(clips_by_id["format-first"].processing_status, "pending")
            self.assertEqual(clips_by_id["format-second"].processing_status, "ready")

    def test_maximum_clips_per_run_limits_formatting_work(self) -> None:
        """The formatter only attempts the configured number of pending clips per pass."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clips = [
                make_clip(f"format-limit-{index}", self.create_source_file(root, f"format-limit-{index}"))
                for index in range(3)
            ]
            metadata_file, _, config = self.make_environment(clips, maximum_clips=2)
            client = FakeFfmpegClient()

            summary = self.make_formatter(metadata_file, config, client).run()

            self.assertEqual(summary.pending, 3)
            self.assertEqual(summary.formatted, 2)
            self.assertEqual(len(client.format_requests), 2)

    def test_source_without_audio_still_formats(self) -> None:
        """Audio-free source media remains a valid vertical rendering input."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "format-silent")
            metadata_file, _, config = self.make_environment([make_clip("format-silent", source_file)])
            client = FakeFfmpegClient(
                inspections={
                    source_file: InputMediaProperties(width=640, height=480, has_audio=False)
                }
            )

            summary = self.make_formatter(metadata_file, config, client).run()

            self.assertEqual(summary.formatted, 1)
            self.assertFalse(client.format_requests[0].input_properties.has_audio)
