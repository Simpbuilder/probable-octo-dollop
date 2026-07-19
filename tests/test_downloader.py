"""Offline tests for pending media download orchestration and retry behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.models import ClipMetadata, DownloaderConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from downloader.downloader import PendingClipDownloader
from downloader.models import DownloadRequest, DownloadResult, MediaInspection
from downloader.utils import safe_filename_stem
from downloader.yt_dlp_client import UnsupportedMediaError, YtDlpClientError


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
DEFAULT_INSPECTION = MediaInspection(
    duration_seconds=20.0,
    width=720,
    height=1280,
    extension="mp4",
    requires_ffmpeg=False,
)


class FakeMediaClient:
    """A deterministic yt-dlp-like client that only writes test files locally."""

    def __init__(
        self,
        inspections: dict[str, MediaInspection | Exception] | None = None,
        download_outcomes: dict[str, DownloadResult | Exception] | None = None,
    ) -> None:
        """Configure per-URL inspection and download outcomes."""
        self.inspections = inspections or {}
        self.download_outcomes = download_outcomes or {}
        self.inspect_requests: list[DownloadRequest] = []
        self.download_requests: list[DownloadRequest] = []

    def inspect(self, request: DownloadRequest) -> MediaInspection:
        """Return a configured inspection result without contacting a remote URL."""
        self.inspect_requests.append(request)
        outcome = self.inspections.get(request.source_url, DEFAULT_INSPECTION)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def download(self, request: DownloadRequest, inspection: MediaInspection) -> DownloadResult:
        """Write a small local MP4 placeholder or raise a configured failure."""
        self.download_requests.append(request)
        outcome = self.download_outcomes.get(request.source_url)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, DownloadResult):
            return outcome

        request.output_directory.mkdir(parents=True, exist_ok=True)
        local_file_path = request.output_directory / f"{request.filename_stem}.mp4"
        local_file_path.write_bytes(b"test media")
        return DownloadResult(
            local_file_path=local_file_path,
            duration_seconds=inspection.duration_seconds,
            width=inspection.width,
            height=inspection.height,
        )


def make_clip(unique_id: str, source_url: str) -> ClipMetadata:
    """Build a pending manual metadata entry suitable for downloader tests."""
    return ClipMetadata(
        unique_id=unique_id,
        source="manual",
        subreddit="funny",
        source_post_id=unique_id,
        source_url=source_url,
        title=f"Manual URL: {source_url}",
        author="manual_intake",
        score=0,
        comment_count=0,
        created_at=NOW,
        media_url=None,
        local_file_path=None,
        download_status="pending",
        processing_status="pending",
        added_at=NOW,
    )


class PendingClipDownloaderTests(unittest.TestCase):
    """Verify downloads update metadata without making live requests."""

    def make_environment(self, clips: list[ClipMetadata], *, downloads_per_run: int = 5):
        """Create isolated metadata and pending-file paths for one test."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        metadata_file = root / "metadata" / "clips.json"
        pending_directory = root / "clips" / "pending"
        for clip in clips:
            save_clip_metadata(metadata_file, clip)
        config = DownloaderConfig(
            directory=pending_directory,
            preferred_format="mp4",
            maximum_duration_seconds=90,
            maximum_file_size_bytes=100_000_000,
            retries=2,
            timeout_seconds=30,
            overwrite=False,
            downloads_per_run=downloads_per_run,
            enabled=False,
        )
        return metadata_file, pending_directory, config

    def make_downloader(self, metadata_file: Path, config: DownloaderConfig, client: FakeMediaClient, *, ffmpeg: bool = True) -> PendingClipDownloader:
        """Build a quiet downloader with an injectable FFmpeg availability result."""
        logger = logging.getLogger(f"test_downloader_{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return PendingClipDownloader(
            metadata_file=metadata_file,
            config=config,
            media_client=client,
            ffmpeg_available=lambda: ffmpeg,
            logger=logger,
        )

    def test_successful_download_updates_metadata(self) -> None:
        """A completed yt-dlp result updates path, dimensions, duration, and status."""
        clip = make_clip("manual-success", "https://example.invalid/success")
        metadata_file, pending_directory, config = self.make_environment([clip])
        client = FakeMediaClient()

        summary = self.make_downloader(metadata_file, config, client).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.pending, 1)
        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(updated_clip.download_status, "downloaded")
        self.assertEqual(updated_clip.processing_status, "pending")
        self.assertIsNone(updated_clip.download_error)
        self.assertEqual(updated_clip.duration_seconds, 20.0)
        self.assertEqual(updated_clip.width, 720)
        self.assertEqual(updated_clip.height, 1280)
        self.assertEqual(updated_clip.local_file_path, (pending_directory / "manual-success.mp4").resolve())
        self.assertTrue(updated_clip.local_file_path.is_file())

    def test_safe_filename_generation_handles_windows_reserved_and_invalid_characters(self) -> None:
        """Download target names cannot escape directories or use Windows-reserved names."""
        self.assertEqual(safe_filename_stem("CON"), "_CON")
        safe_name = safe_filename_stem("manual/clip:<bad>?* name")
        self.assertEqual(safe_name, "manual_clip__bad____name")
        self.assertNotIn("/", safe_name)
        self.assertNotIn("\\", safe_name)

        long_name = "a" * 121
        shortened_name = safe_filename_stem(long_name)
        self.assertEqual(len(shortened_name), 120)
        self.assertNotEqual(shortened_name, safe_filename_stem("b" + long_name[1:]))

    def test_existing_local_file_is_not_overwritten(self) -> None:
        """An orphaned completed file is reconciled without calling yt-dlp again."""
        clip = make_clip("manual-existing", "https://example.invalid/existing")
        metadata_file, pending_directory, config = self.make_environment([clip])
        pending_directory.mkdir(parents=True)
        existing_file = pending_directory / "manual-existing.mp4"
        existing_file.write_bytes(b"already downloaded")
        client = FakeMediaClient()

        summary = self.make_downloader(metadata_file, config, client).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.downloaded, 0)
        self.assertEqual(updated_clip.download_status, "downloaded")
        self.assertEqual(updated_clip.local_file_path, existing_file.resolve())
        self.assertEqual(client.inspect_requests, [])
        self.assertEqual(existing_file.read_bytes(), b"already downloaded")

    def test_unsupported_url_is_skipped_and_remains_pending(self) -> None:
        """Unsupported or image-only media is recorded as a retryable skipped entry."""
        clip = make_clip("manual-unsupported", "https://example.invalid/image")
        metadata_file, _, config = self.make_environment([clip])
        client = FakeMediaClient(
            inspections={clip.source_url: UnsupportedMediaError("No downloadable video was found.")}
        )

        summary = self.make_downloader(metadata_file, config, client).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(updated_clip.download_status, "pending")
        self.assertEqual(updated_clip.download_error, "No downloadable video was found.")

    def test_yt_dlp_failure_keeps_clip_pending_for_retry(self) -> None:
        """A network-like yt-dlp failure stores an error without changing queue state."""
        clip = make_clip("manual-failure", "https://example.invalid/failure")
        metadata_file, _, config = self.make_environment([clip])
        client = FakeMediaClient(
            download_outcomes={clip.source_url: YtDlpClientError("Network request failed")}
        )

        summary = self.make_downloader(metadata_file, config, client).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.failed, 1)
        self.assertEqual(updated_clip.download_status, "pending")
        self.assertEqual(updated_clip.download_error, "Network request failed")

    def test_missing_ffmpeg_skips_merge_required_download(self) -> None:
        """Separate video and audio streams get a clear FFmpeg prerequisite message."""
        clip = make_clip("manual-needs-ffmpeg", "https://example.invalid/merge")
        metadata_file, _, config = self.make_environment([clip])
        client = FakeMediaClient(
            inspections={
                clip.source_url: MediaInspection(
                    duration_seconds=20.0,
                    width=720,
                    height=1280,
                    extension="mp4",
                    requires_ffmpeg=True,
                )
            }
        )

        summary = self.make_downloader(metadata_file, config, client, ffmpeg=False).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.skipped, 1)
        self.assertIn("FFmpeg", updated_clip.download_error)
        self.assertEqual(client.download_requests, [])

    def test_media_over_duration_limit_is_skipped_before_download(self) -> None:
        """Known media longer than the configured duration limit remains retryable."""
        clip = make_clip("manual-too-long", "https://example.invalid/too-long")
        metadata_file, _, config = self.make_environment([clip])
        client = FakeMediaClient(
            inspections={
                clip.source_url: replace(DEFAULT_INSPECTION, duration_seconds=91.0)
            }
        )

        summary = self.make_downloader(metadata_file, config, client).run()

        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(summary.skipped, 1)
        self.assertIn("maximum download duration", updated_clip.download_error)
        self.assertEqual(client.download_requests, [])

    def test_partial_download_artifact_does_not_block_retry(self) -> None:
        """An interrupted yt-dlp `.part` file is not mistaken for completed media."""
        clip = make_clip("manual-partial", "https://example.invalid/partial")
        metadata_file, pending_directory, config = self.make_environment([clip])
        pending_directory.mkdir(parents=True)
        (pending_directory / "manual-partial.mp4.part").write_bytes(b"partial media")
        client = FakeMediaClient()

        summary = self.make_downloader(metadata_file, config, client).run()

        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(len(client.download_requests), 1)
        self.assertTrue((pending_directory / "manual-partial.mp4.part").is_file())

    def test_one_failed_clip_does_not_stop_later_downloads(self) -> None:
        """A failed source does not prevent a later pending clip from downloading."""
        first_clip = make_clip("manual-first", "https://example.invalid/first")
        second_clip = make_clip("manual-second", "https://example.invalid/second")
        metadata_file, _, config = self.make_environment([first_clip, second_clip])
        client = FakeMediaClient(
            download_outcomes={first_clip.source_url: YtDlpClientError("Temporary outage")}
        )

        summary = self.make_downloader(metadata_file, config, client).run()
        clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(clips_by_id[first_clip.unique_id].download_status, "pending")
        self.assertEqual(clips_by_id[second_clip.unique_id].download_status, "downloaded")

    def test_maximum_downloads_per_run_limits_queue_work(self) -> None:
        """Only the configured number of pending clips is attempted in one run."""
        clips = [
            make_clip(f"manual-limit-{index}", f"https://example.invalid/{index}")
            for index in range(3)
        ]
        metadata_file, _, config = self.make_environment(clips, downloads_per_run=2)
        client = FakeMediaClient()

        summary = self.make_downloader(metadata_file, config, client).run()
        stored_clips = load_all_clip_metadata(metadata_file)

        self.assertEqual(summary.pending, 3)
        self.assertEqual(summary.downloaded, 2)
        self.assertEqual(len(client.download_requests), 2)
        self.assertEqual(
            sum(clip.download_status == "pending" for clip in stored_clips),
            1,
        )
