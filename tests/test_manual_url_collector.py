"""Offline tests for manual URL intake and queue bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collector.manual_url_collector import ManualUrlCollector, normalize_manual_url
from collector.models import CollectorConfig, DownloaderConfig, FormatterConfig
from collector.storage import load_all_clip_metadata
from run_pipeline import (
    parse_arguments,
    selected_collectors,
    should_run_collectors,
    should_run_downloader,
    should_run_formatter,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


class ManualUrlCollectorTests(unittest.TestCase):
    """Verify manual queue URLs are stored safely without downloading media."""

    def make_collector(self, contents: str):
        """Create a temporary queue, processed log, and metadata store for a test."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        input_file = root / "input_urls.txt"
        input_file.write_text(contents, encoding="utf-8")
        return (
            ManualUrlCollector(
                input_file=input_file,
                processed_file=root / "metadata" / "processed_urls.txt",
                metadata_file=root / "metadata" / "clips.json",
                clock=lambda: NOW,
            ),
            input_file,
            root / "metadata" / "processed_urls.txt",
            root / "metadata" / "clips.json",
        )

    def test_imports_valid_reddit_url(self) -> None:
        """A Reddit URL keeps its original source URL and gains a stable ID."""
        original_url = "HTTPS://WWW.Reddit.com/r/funny/comments/abc123/example/#fragment"
        collector, input_file, processed_file, metadata_file = self.make_collector(f"{original_url}\n")

        summary = collector.collect()
        clips = load_all_clip_metadata(metadata_file)

        self.assertEqual(summary.accepted, 1)
        self.assertEqual(input_file.read_text(encoding="utf-8"), "")
        self.assertEqual(processed_file.read_text(encoding="utf-8"), f"{original_url}\n")
        self.assertEqual(clips[0].source, "manual")
        self.assertEqual(clips[0].source_url, original_url)
        self.assertEqual(clips[0].subreddit, "funny")
        self.assertIsNone(clips[0].media_url)
        self.assertEqual(clips[0].unique_id, create_expected_id(original_url))

    def test_imports_valid_non_reddit_url(self) -> None:
        """A valid non-Reddit URL is imported while retaining its non-Reddit identity."""
        collector, _, _, metadata_file = self.make_collector("https://example.com/video?id=7\n")

        summary = collector.collect()
        clip = load_all_clip_metadata(metadata_file)[0]

        self.assertEqual(summary.accepted, 1)
        self.assertIsNone(clip.subreddit)
        self.assertEqual(clip.source, "manual")

    def test_invalid_url_stays_in_queue(self) -> None:
        """Non-HTTP(S) URLs are counted as invalid and retained for correction."""
        collector, input_file, processed_file, _ = self.make_collector("ftp://example.com/video\n")

        summary = collector.collect()

        self.assertEqual(summary.invalid_urls, 1)
        self.assertEqual(input_file.read_text(encoding="utf-8"), "ftp://example.com/video\n")
        self.assertFalse(processed_file.exists())

    def test_blank_and_comment_lines_are_ignored_and_preserved(self) -> None:
        """Only URL entries are counted; comments and blank lines remain in the queue."""
        contents = "# First comment\n\nhttps://example.com/video\n  # Second comment\n"
        collector, input_file, _, _ = self.make_collector(contents)

        summary = collector.collect()

        self.assertEqual(summary.urls_found, 1)
        self.assertEqual(summary.accepted, 1)
        self.assertEqual(input_file.read_text(encoding="utf-8"), "# First comment\n\n  # Second comment\n")

    def test_duplicate_url_is_removed_without_creating_second_metadata_record(self) -> None:
        """A repeated normalized URL resolves as a duplicate instead of retrying forever."""
        url = "https://example.com/video"
        collector, input_file, processed_file, metadata_file = self.make_collector(f"{url}\n")
        collector.collect()
        input_file.write_text(f"{url}\n", encoding="utf-8")

        summary = collector.collect()

        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(len(load_all_clip_metadata(metadata_file)), 1)
        self.assertEqual(input_file.read_text(encoding="utf-8"), "")
        self.assertEqual(processed_file.read_text(encoding="utf-8"), f"{url}\n{url}\n")

    def test_failed_url_remains_for_retry(self) -> None:
        """A storage failure does not remove the URL from the local queue."""
        url = "https://example.com/video"
        collector, input_file, processed_file, _ = self.make_collector(f"{url}\n")

        with patch(
            "collector.manual_url_collector.save_clip_metadata", side_effect=OSError("disk full")
        ):
            summary = collector.collect()

        self.assertEqual(summary.errors, 1)
        self.assertEqual(input_file.read_text(encoding="utf-8"), f"{url}\n")
        self.assertFalse(processed_file.exists())


class PipelineModeTests(unittest.TestCase):
    """Verify pipeline modes select the intended collector combinations."""

    def test_selects_collectors_for_each_pipeline_mode(self) -> None:
        """Manual, Reddit, and combined modes retain independent execution paths."""
        self.assertEqual(selected_collectors("manual_urls"), ("manual_urls",))
        self.assertEqual(selected_collectors("reddit_api"), ("reddit_api",))
        self.assertEqual(selected_collectors("both"), ("manual_urls", "reddit_api"))

    def test_downloader_requires_explicit_opt_in_when_disabled(self) -> None:
        """Normal manual intake does not download URLs unless enabled or flagged."""
        disabled_config = CollectorConfig(
            source_configs={},
            output_folders={},
            metadata_file=Path("metadata/clips.json"),
            downloader_config=DownloaderConfig(directory=Path("clips/pending"), enabled=False),
        )
        enabled_config = CollectorConfig(
            source_configs={},
            output_folders={},
            metadata_file=Path("metadata/clips.json"),
            downloader_config=DownloaderConfig(directory=Path("clips/pending"), enabled=True),
            formatter_config=FormatterConfig(output_directory=Path("clips/ready")),
        )

        self.assertFalse(should_run_downloader(disabled_config, explicit_download=False))
        self.assertTrue(should_run_downloader(disabled_config, explicit_download=True))
        self.assertTrue(should_run_downloader(enabled_config, explicit_download=False))
        self.assertTrue(parse_arguments(["--download"]).download)

    def test_formatter_flags_keep_format_only_runs_separate_from_collection_and_downloads(self) -> None:
        """A format-only run does not implicitly collect or download new media."""
        disabled_config = CollectorConfig(
            source_configs={},
            output_folders={},
            metadata_file=Path("metadata/clips.json"),
            downloader_config=DownloaderConfig(directory=Path("clips/pending"), enabled=True),
            formatter_config=FormatterConfig(output_directory=Path("clips/ready"), enabled=False),
        )
        enabled_config = CollectorConfig(
            source_configs={},
            output_folders={},
            metadata_file=Path("metadata/clips.json"),
            formatter_config=FormatterConfig(output_directory=Path("clips/ready"), enabled=True),
        )

        self.assertTrue(parse_arguments(["--format"]).format)
        self.assertTrue(parse_arguments(["--format-one"]).format_one)
        self.assertTrue(parse_arguments(["--download", "--format"]).download)
        self.assertTrue(parse_arguments(["--download", "--format"]).format)
        self.assertFalse(should_run_collectors(explicit_download=False, explicit_format=True))
        self.assertTrue(should_run_collectors(explicit_download=True, explicit_format=True))
        self.assertFalse(
            should_run_downloader(disabled_config, explicit_download=False, format_only=True)
        )
        self.assertTrue(should_run_formatter(disabled_config, explicit_format=True))
        self.assertTrue(should_run_formatter(enabled_config, explicit_format=False))


def create_expected_id(original_url: str) -> str:
    """Use the public normalizer to verify URL identity stays stable in storage."""
    import hashlib

    normalized_url = normalize_manual_url(original_url).normalized_url
    return f"manual-{hashlib.sha256(normalized_url.encode('utf-8')).hexdigest()}"
