"""Tests for JSON collector configuration loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.config import ConfigurationError, load_collector_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CollectorConfigTests(unittest.TestCase):
    """Verify the checked-in JSON configuration has a usable typed form."""

    def test_loads_checked_in_configuration(self) -> None:
        """The project configuration exposes Reddit and its resolved paths."""
        config = load_collector_config(PROJECT_ROOT / "config")

        self.assertEqual(config.enabled_sources, ("reddit",))
        self.assertEqual(config.source_configs["reddit"].minimum_score, 500)
        self.assertEqual(config.output_path("pending"), PROJECT_ROOT / "clips" / "pending")
        self.assertEqual(config.metadata_file, PROJECT_ROOT / "metadata" / "clips.json")
        self.assertEqual(config.pipeline_mode, "manual_urls")
        self.assertEqual(config.manual_urls_per_run, 50)
        self.assertIsNotNone(config.downloader_config)
        self.assertEqual(config.downloader_config.directory, PROJECT_ROOT / "clips" / "pending")
        self.assertFalse(config.downloader_config.enabled)
        self.assertEqual(config.downloader_config.downloads_per_run, 50)
        self.assertIsNotNone(config.formatter_config)
        self.assertEqual(config.formatter_config.output_directory, PROJECT_ROOT / "clips" / "ready")
        self.assertFalse(config.formatter_config.enabled)
        self.assertEqual(config.formatter_config.maximum_clips_per_run, 50)
        self.assertTrue(config.formatter_config.hook.enabled)
        self.assertIsNone(config.formatter_config.hook.font_path)
        self.assertEqual(config.formatter_config.hook.horizontal_alignment, "center")
        self.assertIsNotNone(config.hook_generation_config)
        self.assertFalse(config.hook_generation_config.enabled)
        self.assertFalse(config.hook_generation_config.automatic_selection)
        self.assertEqual(config.hook_generation_config.maximum_clips_per_run, 50)
        self.assertIn("what happens next", config.hook_generation_config.blocked_phrases)
        self.assertIsNotNone(config.instagram_config)
        self.assertIsInstance(config.instagram_config.enabled, bool)
        self.assertEqual(
            config.instagram_config.source_directory,
            PROJECT_ROOT / "clips" / "ready" / "hooked",
        )
        self.assertIn(config.instagram_config.publish_mode, {"draft", "publish_now"})
        self.assertGreater(config.instagram_config.maximum_uploads_per_run, 0)
        self.assertTrue(config.instagram_config.delay_between_posts_enabled)
        self.assertEqual(config.instagram_config.delay_between_posts_seconds, 30)
        self.assertEqual(config.instagram_config.maximum_delay_seconds, 300)
        self.assertIsNotNone(config.youtube_config)
        self.assertTrue(config.youtube_config.enabled)
        self.assertEqual(
            config.youtube_config.source_directory,
            PROJECT_ROOT / "clips" / "ready" / "hooked",
        )
        self.assertEqual(config.youtube_config.privacy_status, "public")
        self.assertFalse(config.youtube_config.made_for_kids)
        self.assertEqual(config.youtube_config.maximum_uploads_per_run, 50)
        self.assertEqual(
            config.youtube_config.oauth_client_credentials_file,
            PROJECT_ROOT / "client_secret.json",
        )
        self.assertEqual(config.youtube_config.token_file, PROJECT_ROOT / "token.json")
        self.assertIsNotNone(config.archive_config)
        self.assertTrue(config.archive_config.enabled)
        self.assertEqual(
            config.archive_config.archive_directory,
            PROJECT_ROOT / "clips" / "archive" / "hooked",
        )
        self.assertTrue(config.archive_config.verify_copy)

    def test_rejects_enabled_reddit_without_subreddits(self) -> None:
        """Validation catches a missing Reddit target list before a run starts."""
        with TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            config_directory = project_root / "config"
            config_directory.mkdir()
            (config_directory / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": {
                            "reddit": {
                                "enabled": True,
                                "subreddits": [],
                                "minimum_score": 1,
                                "maximum_clip_length_seconds": 90,
                                "maximum_post_age_days": 1,
                                "sorting_mode": "hot",
                                "posts_to_inspect": 10,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (config_directory / "collector.json").write_text(
                json.dumps(
                    {
                        "output_folders": {
                            "pending": "clips/pending",
                            "approved": "clips/approved",
                            "rejected": "clips/rejected",
                            "ready": "clips/ready",
                            "posted": "clips/posted",
                            "metadata": "metadata",
                        },
                        "metadata_file": "metadata/clips.json",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigurationError):
                load_collector_config(config_directory)
