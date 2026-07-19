"""Offline queue tests for vertical metadata updates and failure isolation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.models import ClipMetadata, FormatterConfig, HookConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from formatter.ffmpeg_client import FfmpegClientError, FfmpegDependencyError
from formatter.formatter import PendingClipFormatter
from formatter.hooks import HookRenderError, HookRenderResult, HookSelection
from formatter.models import FormatRequest, FormatResult, InputMediaProperties
from formatter.utils import formatted_output_path


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


class FakeHookRenderer:
    """A deterministic hook renderer that only creates a disposable placeholder overlay."""

    def __init__(self, outcomes: dict[str, Exception] | None = None) -> None:
        """Configure one rendering outcome by selected hook text."""
        self.outcomes = outcomes or {}
        self.selections: list[HookSelection] = []

    def render(
        self,
        selection: HookSelection,
        config: HookConfig,
        *,
        canvas_width: int,
        canvas_height: int,
        overlay_file: Path,
    ) -> HookRenderResult:
        """Write an inert temporary overlay or raise the configured hook failure."""
        del config, canvas_width, canvas_height
        self.selections.append(selection)
        outcome = self.outcomes.get(selection.text)
        if outcome is not None:
            raise outcome
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_bytes(b"hook overlay")
        return HookRenderResult(
            overlay_file=overlay_file,
            text=selection.text,
            source=selection.source,
            status="rendered",
        )


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

    def make_environment(
        self,
        clips: list[ClipMetadata],
        *,
        maximum_clips: int = 5,
        hook_config: HookConfig | None = None,
    ):
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
            hook=hook_config or HookConfig(),
        )
        return metadata_file, ready_directory, config

    def make_formatter(
        self,
        metadata_file: Path,
        config: FormatterConfig,
        client: FakeFfmpegClient,
        hook_renderer: FakeHookRenderer | None = None,
    ) -> PendingClipFormatter:
        """Build a quiet formatter whose external behavior is fully injected."""
        logger = logging.getLogger(f"test_formatter_{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return PendingClipFormatter(
            metadata_file,
            config,
            client,
            hook_renderer=hook_renderer,
            logger=logger,
        )

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
            self.assertEqual(
                updated_clip.formatted_file_path,
                formatted_output_path(ready_directory, clip.unique_id).resolve(),
            )
            self.assertEqual(updated_clip.formatted_file_path.parent.name, "plain")
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
            self.assertEqual(
                updated_clip.formatted_file_path,
                formatted_output_path(ready_directory, clip.unique_id).resolve(),
            )

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
            self.assertEqual(updated_clip.formatted_file_path, output_file.resolve())

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

    def test_stored_manual_hook_updates_metadata_and_uses_a_separate_ready_variant(self) -> None:
        """A stored hook becomes the recorded rendered text without replacing source media."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-manual")
            hook_text = "He looked away for one second..."
            clip = replace(
                make_clip("hook-manual", source_file),
                hook_text=hook_text,
                hook_source="manual",
            )
            metadata_file, ready_directory, config = self.make_environment(
                [clip],
                hook_config=HookConfig(enabled=True),
            )
            renderer = FakeHookRenderer()

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                renderer,
            ).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(renderer.selections, [HookSelection(hook_text, "manual")])
            self.assertEqual(updated_clip.hook_text, hook_text)
            self.assertEqual(updated_clip.hook_source, "manual")
            self.assertEqual(updated_clip.hook_status, "rendered")
            self.assertIsNone(updated_clip.hook_error)
            self.assertEqual(
                updated_clip.formatted_file_path,
                formatted_output_path(ready_directory, clip.unique_id, hook_text).resolve(),
            )
            self.assertEqual(updated_clip.formatted_file_path.parent.name, "hooked")

    def test_source_title_is_used_when_no_manual_hook_is_stored(self) -> None:
        """The configured title fallback is explicit in metadata after a successful render."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-title")
            clip = make_clip("hook-title", source_file)
            metadata_file, _, config = self.make_environment(
                [clip],
                hook_config=HookConfig(enabled=True, fallback_to_source_title=True),
            )
            renderer = FakeHookRenderer()

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                renderer,
            ).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(renderer.selections, [HookSelection(clip.title, "source_title")])
            self.assertEqual(updated_clip.hook_text, clip.title)
            self.assertEqual(updated_clip.hook_source, "source_title")
            self.assertEqual(updated_clip.hook_status, "rendered")
            self.assertEqual(updated_clip.formatted_file_path.parent.name, "hooked")

    def test_no_hook_text_formats_normally_when_title_fallback_is_disabled(self) -> None:
        """A no-hook clip still follows the original formatter path and output name."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-none")
            clip = make_clip("hook-none", source_file)
            metadata_file, ready_directory, config = self.make_environment(
                [clip],
                hook_config=HookConfig(enabled=True, fallback_to_source_title=False),
            )
            renderer = FakeHookRenderer()

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                renderer,
            ).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(renderer.selections, [])
            self.assertEqual(updated_clip.hook_status, "skipped")
            self.assertEqual(
                updated_clip.formatted_file_path,
                formatted_output_path(ready_directory, clip.unique_id).resolve(),
            )
            self.assertEqual(updated_clip.formatted_file_path.parent.name, "plain")

    def test_plain_and_hooked_outputs_use_separate_ready_directories(self) -> None:
        """No-hook and hook renders retain their filenames in separate ready subdirectories."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plain_clip = make_clip("plain-output", self.create_source_file(root, "plain-output"))
            hook_text = "A clear hook"
            hooked_clip = replace(
                make_clip("hooked-output", self.create_source_file(root, "hooked-output")),
                hook_text=hook_text,
                hook_source="manual",
            )
            metadata_file, ready_directory, config = self.make_environment(
                [plain_clip, hooked_clip],
                hook_config=HookConfig(enabled=True, fallback_to_source_title=False),
            )

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                FakeHookRenderer(),
            ).run()

            clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}
            plain_output = clips_by_id[plain_clip.unique_id].formatted_file_path
            hooked_output = clips_by_id[hooked_clip.unique_id].formatted_file_path
            self.assertEqual(summary.formatted, 2)
            self.assertEqual(
                plain_output,
                formatted_output_path(ready_directory, plain_clip.unique_id).resolve(),
            )
            self.assertEqual(
                hooked_output,
                formatted_output_path(ready_directory, hooked_clip.unique_id, hook_text).resolve(),
            )
            self.assertEqual(plain_output.parent, ready_directory / "plain")
            self.assertEqual(hooked_output.parent, ready_directory / "hooked")
            self.assertTrue(plain_output.is_file())
            self.assertTrue(hooked_output.is_file())

    def test_disabled_hooks_preserve_stored_manual_text_without_rendering_it(self) -> None:
        """Turning hooks off preserves a later-use manual hook while rendering the base video."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-disabled")
            clip = replace(
                make_clip("hook-disabled", source_file),
                hook_text="Keep this for later",
                hook_source="manual",
            )
            metadata_file, _, config = self.make_environment(
                [clip],
                hook_config=HookConfig(enabled=False),
            )
            renderer = FakeHookRenderer()

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                renderer,
            ).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(renderer.selections, [])
            self.assertEqual(updated_clip.hook_text, "Keep this for later")
            self.assertEqual(updated_clip.hook_status, "skipped")

    def test_hook_render_failure_does_not_stop_later_clips(self) -> None:
        """One hook overlay failure leaves its clip retryable while the next one succeeds."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_source = self.create_source_file(root, "hook-first")
            second_source = self.create_source_file(root, "hook-second")
            first_clip = make_clip("hook-first", first_source)
            second_clip = make_clip("hook-second", second_source)
            metadata_file, _, config = self.make_environment(
                [first_clip, second_clip],
                hook_config=HookConfig(enabled=True),
            )
            renderer = FakeHookRenderer(
                outcomes={first_clip.title: HookRenderError("font renderer failed")}
            )

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                renderer,
            ).run()
            clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(clips_by_id[first_clip.unique_id].processing_status, "pending")
            self.assertEqual(clips_by_id[first_clip.unique_id].hook_status, "failed")
            self.assertIn("font renderer failed", clips_by_id[first_clip.unique_id].hook_error)
            self.assertEqual(clips_by_id[second_clip.unique_id].hook_status, "rendered")

    def test_ffmpeg_failure_after_hook_render_keeps_hook_retryable(self) -> None:
        """A composition failure records the selected hook as failed instead of ready."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-ffmpeg-failure")
            clip = make_clip("hook-ffmpeg-failure", source_file)
            metadata_file, _, config = self.make_environment(
                [clip],
                hook_config=HookConfig(enabled=True),
            )
            client = FakeFfmpegClient(
                format_outcomes={source_file: FfmpegClientError("overlay encoding failed")}
            )

            summary = self.make_formatter(
                metadata_file,
                config,
                client,
                FakeHookRenderer(),
            ).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.failed, 1)
            self.assertEqual(updated_clip.processing_status, "pending")
            self.assertEqual(updated_clip.hook_status, "failed")
            self.assertIn("overlay encoding failed", updated_clip.hook_error)

    def test_manual_hook_can_reformat_a_ready_clip_without_overwriting_its_reference_output(self) -> None:
        """A one-clip override writes a digest-suffixed hook render beside the original output."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = self.create_source_file(root, "hook-validation")
            clip = make_clip("hook-validation", source_file)
            metadata_file, ready_directory, config = self.make_environment(
                [replace(clip, processing_status="ready")],
                hook_config=HookConfig(enabled=True),
            )
            ready_directory.mkdir(parents=True, exist_ok=True)
            reference_output = formatted_output_path(ready_directory, clip.unique_id)
            reference_output.parent.mkdir(parents=True, exist_ok=True)
            reference_output.write_bytes(b"reference ready media")
            hook_text = "He looked away for one second..."

            summary = self.make_formatter(
                metadata_file,
                config,
                FakeFfmpegClient(),
                FakeHookRenderer(),
            ).run(manual_hook=hook_text, include_ready_for_manual_hook=True)

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            hook_output = formatted_output_path(ready_directory, clip.unique_id, hook_text).resolve()
            self.assertEqual(summary.pending, 1)
            self.assertEqual(summary.formatted, 1)
            self.assertEqual(reference_output.read_bytes(), b"reference ready media")
            self.assertEqual(updated_clip.formatted_file_path, hook_output)
            self.assertTrue(hook_output.is_file())
