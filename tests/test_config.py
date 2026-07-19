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
