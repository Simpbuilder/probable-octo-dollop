"""Tests for yt-dlp option construction and metadata parsing with a fake backend."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from downloader.models import DownloadRequest
from downloader.yt_dlp_client import UnsupportedMediaError, YtDlpClient


class FakeYoutubeDL:
    """Context-manager-shaped stand-in for yt-dlp's ``YoutubeDL`` class."""

    instances: list["FakeYoutubeDL"] = []

    def __init__(self, options: dict[str, object]) -> None:
        """Keep the options passed to yt-dlp for assertions."""
        self.options = options
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeYoutubeDL":
        """Return the fake context object."""
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> bool:
        """Do not suppress exceptions from the wrapped code."""
        return False

    def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
        """Return a selected multi-stream video record without network activity."""
        self.download_requested = download
        return {
            "vcodec": "avc1",
            "duration": 25,
            "width": 1080,
            "height": 1920,
            "ext": "mp4",
            "format_id": "137+140",
            "requested_formats": [{"format_id": "137"}, {"format_id": "140"}],
        }


class YtDlpClientTests(unittest.TestCase):
    """Verify the adapter's deterministic behavior independently of installed yt-dlp."""

    def test_inspection_prefers_mp4_and_detects_merge_requirement(self) -> None:
        """The client creates merge-capable options and parses useful media properties."""
        FakeYoutubeDL.instances.clear()
        with TemporaryDirectory() as temporary_directory:
            request = DownloadRequest(
                source_url="https://example.invalid/video",
                output_directory=Path(temporary_directory),
                filename_stem="manual-example",
                preferred_format="mp4",
                maximum_file_size_bytes=100_000_000,
                retries=2,
                timeout_seconds=30,
                overwrite=False,
            )
            inspection = YtDlpClient(FakeYoutubeDL).inspect(request)

        options = FakeYoutubeDL.instances[0].options
        self.assertTrue(inspection.requires_ffmpeg)
        self.assertEqual(inspection.duration_seconds, 25.0)
        self.assertEqual(inspection.width, 1080)
        self.assertEqual(inspection.height, 1920)
        self.assertEqual(options["merge_output_format"], "mp4")
        self.assertEqual(options["max_filesize"], 100_000_000)
        self.assertTrue(options["skip_download"])

    def test_inspection_rejects_image_only_result(self) -> None:
        """An extractor result without a video stream is skipped before download."""
        class ImageOnlyYoutubeDL(FakeYoutubeDL):
            """Return a still-image-like result with no video codec."""

            def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
                """Return a non-video record without accessing a network service."""
                self.download_requested = download
                return {"vcodec": "none", "formats": [{"vcodec": "none"}]}

        with TemporaryDirectory() as temporary_directory:
            request = DownloadRequest(
                source_url="https://example.invalid/image",
                output_directory=Path(temporary_directory),
                filename_stem="manual-image",
                preferred_format="mp4",
                maximum_file_size_bytes=None,
                retries=2,
                timeout_seconds=30,
                overwrite=False,
            )
            with self.assertRaises(UnsupportedMediaError):
                YtDlpClient(ImageOnlyYoutubeDL).inspect(request)

    def test_download_locates_the_final_media_file_written_by_yt_dlp(self) -> None:
        """The adapter resolves yt-dlp's extension-preserving completed output."""

        class WritingYoutubeDL(FakeYoutubeDL):
            """Write a small MP4 only for the mocked download invocation."""

            def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
                """Create the final test output while returning normal video metadata."""
                info = super().extract_info(url, download=download)
                if download:
                    output_path = Path(str(self.options["outtmpl"])).with_suffix(".mp4")
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(b"test media")
                return info

        FakeYoutubeDL.instances.clear()
        with TemporaryDirectory() as temporary_directory:
            request = DownloadRequest(
                source_url="https://example.invalid/video",
                output_directory=Path(temporary_directory),
                filename_stem="manual-output",
                preferred_format="mp4",
                maximum_file_size_bytes=None,
                retries=2,
                timeout_seconds=30,
                overwrite=False,
            )
            client = YtDlpClient(WritingYoutubeDL)

            result = client.download(request, client.inspect(request))

            self.assertEqual(result.local_file_path, (Path(temporary_directory) / "manual-output.mp4").resolve())
            self.assertTrue(result.local_file_path.is_file())
            self.assertFalse(WritingYoutubeDL.instances[-1].options["skip_download"])
