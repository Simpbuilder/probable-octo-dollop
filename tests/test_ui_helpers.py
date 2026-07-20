"""Offline tests that keep the Streamlit support layer thin and pipeline-backed."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest
from unittest.mock import patch

from collector import load_collector_config
from ui_helpers import (
    UiConfigurationValues,
    append_unique_urls,
    run_manual_import,
    run_pipeline_action,
    save_ui_configuration,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class UiHelperTests(unittest.TestCase):
    """Verify UI helpers delegate real work to the established runner and services."""

    def test_pipeline_action_delegates_to_existing_runner_main(self) -> None:
        """The UI pipeline button helper invokes run_pipeline.main instead of recreating stages."""
        with patch("run_pipeline.main", return_value=0) as pipeline_main:
            result = run_pipeline_action(["--format", "--all"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.arguments, ("--format", "--all"))
        pipeline_main.assert_called_once_with(("--format", "--all"))

    def test_manual_import_delegates_to_existing_runner_function(self) -> None:
        """The URL import UI helper calls the runner's manual collector rather than parsing metadata itself."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shutil.copytree(PROJECT_ROOT / "config", root / "config")
            (root / "input_urls.txt").write_text("", encoding="utf-8")
            with patch("run_pipeline.run_manual_url_collector", return_value=0) as collector:
                result = run_manual_import(root)

        self.assertEqual(result.exit_code, 0)
        collector.assert_called_once()
        self.assertTrue(collector.call_args.kwargs["process_all"])

    def test_add_urls_preserves_comments_existing_queue_and_avoids_normalized_duplicates(self) -> None:
        """UI URL additions reuse manual URL normalization while leaving existing queue text intact."""
        with TemporaryDirectory() as temporary_directory:
            input_file = Path(temporary_directory) / "input_urls.txt"
            input_file.write_text(
                "# keep this note\nhttps://www.reddit.com/r/funny/comments/example/\n",
                encoding="utf-8",
            )

            result = append_unique_urls(
                input_file,
                "https://www.reddit.com/r/funny/comments/example\nhttps://example.com/new\nnot-a-url\n",
            )

            self.assertEqual(result.added, 1)
            self.assertEqual(result.duplicates, 1)
            self.assertEqual(result.invalid_lines, ("not-a-url",))
            self.assertEqual(
                input_file.read_text(encoding="utf-8"),
                "# keep this note\nhttps://www.reddit.com/r/funny/comments/example/\nhttps://example.com/new\n",
            )

    def test_configuration_save_validates_before_writing_and_preserves_unrelated_settings(self) -> None:
        """The UI config form updates only its allowed fields through the shared full-config validator."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shutil.copytree(PROJECT_ROOT / "config", root / "config")
            original_sources = (root / "config" / "sources.json").read_text(encoding="utf-8")
            values = UiConfigurationValues(
                downloads_per_run=7,
                hook_generations_per_run=8,
                formats_per_run=9,
                uploads_per_run=10,
                instagram_publish_mode="draft",
                instagram_caption="Fixed caption",
                instagram_account_id="account-1",
                automatic_hook_selection=True,
            )

            save_ui_configuration(root, values)
            config = load_collector_config(root / "config")

            self.assertEqual(config.downloader_config.downloads_per_run, 7)
            self.assertEqual(config.hook_generation_config.maximum_clips_per_run, 8)
            self.assertEqual(config.formatter_config.maximum_clips_per_run, 9)
            self.assertEqual(config.instagram_config.maximum_uploads_per_run, 10)
            self.assertEqual(config.instagram_config.account_id, "account-1")
            self.assertTrue(config.hook_generation_config.automatic_selection)
            self.assertEqual((root / "config" / "sources.json").read_text(encoding="utf-8"), original_sources)

            invalid_values = replace(values, uploads_per_run=0)
            with self.assertRaises(ValueError):
                save_ui_configuration(root, invalid_values)
