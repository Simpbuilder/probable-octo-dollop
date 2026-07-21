"""Offline tests that keep the Streamlit support layer thin and pipeline-backed."""

from __future__ import annotations

from dataclasses import replace
import ast
import importlib
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest
from unittest.mock import patch

from collector import load_collector_config
from background_jobs import request_background_job_stop as canonical_request_background_job_stop
from pipeline_runtime import RuntimeStatus, RuntimeStatusStore
from publisher.history import append_post_history, build_post_history_record
from ui_helpers import (
    InstagramOverview,
    UiConfigurationValues,
    append_unique_urls,
    load_instagram_overview,
    load_pipeline_progress,
    load_runtime_status,
    request_background_job_stop,
    run_manual_import,
    run_pipeline_action,
    save_ui_configuration,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_APP_UI_IMPORT_PARAMETERS = {
    "UiConfigurationValues": (
        "downloads_per_run",
        "hook_generations_per_run",
        "formats_per_run",
        "uploads_per_run",
        "instagram_publish_mode",
        "instagram_caption",
        "instagram_account_id",
        "automatic_hook_selection",
        "instagram_delay_enabled",
        "instagram_delay_seconds",
        "instagram_maximum_delay_seconds",
    ),
    "append_unique_urls": ("input_file", "raw_text"),
    "load_dashboard_counts": ("project_root",),
    "load_failed_items": ("config",),
    "load_instagram_overview": ("config",),
    "load_pipeline_progress": ("config", "counts"),
    "load_ready_videos": ("config",),
    "load_reviewable_clips": ("config",),
    "load_system_availability": ("project_root",),
    "load_ui_configuration": ("config",),
    "preview_cleanup": ("project_root", "all_temporary", "reset_project"),
    "reject_review_candidates": ("config", "clip_id"),
    "run_confirmed_cleanup": ("plan",),
    "run_manual_import": ("project_root",),
    "run_instagram_upload_action": (
        "project_root",
        "upload_one",
        "process_all",
        "publish_now",
        "post_delay",
        "progress_callback",
    ),
    "run_pipeline_action": ("arguments", "progress_callback"),
    "resolve_auto_refresh_interval": ("selection", "status"),
    "save_review_custom_hook": ("config", "clip_id", "custom_text"),
    "save_ui_configuration": ("project_root", "values"),
    "select_review_candidate": ("config", "clip_id", "candidate_index"),
    "DashboardCounts": (
        "urls_waiting",
        "pending_metadata",
        "downloaded_clips",
        "awaiting_hook_generation",
        "awaiting_hook_review",
        "ready_hooked_videos",
        "uploaded_or_posted",
        "failed_items",
    ),
    "InstagramOverview": (
        "account_username",
        "publish_mode",
        "fixed_caption",
        "pending_uploads",
        "history_total",
        "drafts",
        "published",
        "delay_enabled",
        "delay_seconds",
        "maximum_delay_seconds",
        "estimated_batch_seconds",
    ),
    "PipelineProgress": (
        "urls_to_import",
        "downloads_to_run",
        "hooks_to_generate",
        "hooks_to_review",
        "formats_to_run",
        "uploads_to_run",
    ),
}


class UiHelperTests(unittest.TestCase):
    """Verify UI helpers delegate real work to the established runner and services."""

    def test_pipeline_action_delegates_to_existing_runner_main(self) -> None:
        """The UI pipeline button helper invokes run_pipeline.main instead of recreating stages."""
        with patch("run_pipeline.main", return_value=0) as pipeline_main:
            result = run_pipeline_action(["--format", "--all"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.arguments, ("--format", "--all"))
        pipeline_main.assert_called_once_with(("--format", "--all"))

    def test_instagram_overview_is_public_and_app_imports_without_annotation_only_types(self) -> None:
        """The Streamlit entry point can import the public summary model without a runtime type dependency."""
        overview = InstagramOverview(
            account_username="creator",
            publish_mode="draft",
            fixed_caption="Caption",
            pending_uploads=2,
            history_total=3,
            drafts=1,
            published=2,
        )

        app = importlib.import_module("app")

        self.assertEqual(overview.account_username, "creator")
        self.assertTrue(callable(app.main))
        self.assertNotIn("InstagramOverview", app.__dict__)

    def test_runtime_status_wrapper_delegates_to_the_canonical_recovery_safe_store(self) -> None:
        """The compatibility helper does not parse state itself and handles missing or bad files safely."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertEqual(load_runtime_status(root), RuntimeStatus.idle())
            status_file = root / "metadata" / "runtime_status.json"
            status_file.parent.mkdir(parents=True)
            status_file.write_text("not json", encoding="utf-8")
            self.assertEqual(load_runtime_status(root), RuntimeStatus.idle())

        expected = RuntimeStatus(status="running", job_id="job-1", stage="Download")
        with patch("ui_helpers.load_runtime_status_file", return_value=expected) as loader:
            self.assertEqual(load_runtime_status(PROJECT_ROOT), expected)
        loader.assert_called_once_with(PROJECT_ROOT / "metadata" / "runtime_status.json")

    def test_all_runtime_ui_helper_imports_in_app_exist(self) -> None:
        """App startup imports the complete supported UI helper contract with expected signatures."""
        ui_helpers = importlib.import_module("ui_helpers")
        app = importlib.import_module("app")
        app_tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
        imported_names = {
            alias.name
            for node in ast.walk(app_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "ui_helpers"
            for alias in node.names
        }

        self.assertTrue(callable(app.main))
        self.assertSetEqual(imported_names, set(EXPECTED_APP_UI_IMPORT_PARAMETERS))
        self.assertTrue(all(hasattr(ui_helpers, name) for name in imported_names))
        for name in imported_names:
            parameters = tuple(inspect.signature(getattr(ui_helpers, name)).parameters)
            self.assertEqual(parameters, EXPECTED_APP_UI_IMPORT_PARAMETERS[name], name)
        self.assertNotIn("runtime_status_file", imported_names)

    def test_background_stop_uses_the_canonical_cancellation_request(self) -> None:
        """The UI compatibility export delegates to the one durable background-job mechanism."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = RuntimeStatusStore(root / "metadata" / "runtime_status.json")
            original = store.write(
                RuntimeStatus(
                    job_id="job-1",
                    stage="Download",
                    status="running",
                    completed_count=2,
                    total_count=5,
                    failed_count=1,
                    current_item="clip-4",
                )
            )

            stopped = canonical_request_background_job_stop(root)

            self.assertIs(request_background_job_stop, canonical_request_background_job_stop)
            self.assertTrue(stopped.cancel_requested)
            self.assertEqual(stopped.completed_count, original.completed_count)
            self.assertEqual(stopped.failed_count, original.failed_count)
            self.assertEqual(stopped.current_item, original.current_item)

    def test_app_uses_the_canonical_runtime_status_loader(self) -> None:
        """App runtime reads bypass the UI helper module's public import surface."""
        app = importlib.import_module("app")
        expected = RuntimeStatus(status="running", job_id="job-1", stage="Download")

        with patch("app.load_runtime_status_file", return_value=expected) as loader:
            self.assertEqual(app._load_runtime_status(), expected)

        loader.assert_called_once_with(PROJECT_ROOT / "metadata" / "runtime_status.json")

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
                instagram_delay_enabled=True,
                instagram_delay_seconds=60,
                instagram_maximum_delay_seconds=300,
            )

            save_ui_configuration(root, values)
            config = load_collector_config(root / "config")

            self.assertEqual(config.downloader_config.downloads_per_run, 7)
            self.assertEqual(config.hook_generation_config.maximum_clips_per_run, 8)
            self.assertEqual(config.formatter_config.maximum_clips_per_run, 9)
            self.assertEqual(config.instagram_config.maximum_uploads_per_run, 10)
            self.assertEqual(config.instagram_config.account_id, "account-1")
            self.assertTrue(config.hook_generation_config.automatic_selection)
            self.assertEqual(config.instagram_config.delay_between_posts_seconds, 60)
            self.assertEqual((root / "config" / "sources.json").read_text(encoding="utf-8"), original_sources)

            invalid_values = replace(values, uploads_per_run=0)
            with self.assertRaises(ValueError):
                save_ui_configuration(root, invalid_values)

    def test_pipeline_progress_and_instagram_overview_are_read_only_local_summaries(self) -> None:
        """The polished UI can count queues and history without invoking the uploader or any network client."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shutil.copytree(PROJECT_ROOT / "config", root / "config")
            (root / "input_urls.txt").write_text("https://example.com/queued\n", encoding="utf-8")
            ready_directory = root / "clips" / "ready" / "hooked"
            ready_directory.mkdir(parents=True)
            uploaded_file = ready_directory / "uploaded.mp4"
            pending_file = ready_directory / "pending.mp4"
            uploaded_file.write_bytes(b"video")
            pending_file.write_bytes(b"video")
            append_post_history(
                root / "metadata" / "zernio_post_history.json",
                build_post_history_record(
                    post_id="post-1",
                    status="draft",
                    account_id="account-1",
                    filename=uploaded_file.name,
                    public_media_url="https://media.example/uploaded.mp4",
                    publish_mode="draft",
                ),
            )

            config = load_collector_config(root / "config")
            progress = load_pipeline_progress(config)
            overview = load_instagram_overview(config)

            self.assertEqual(progress.urls_to_import, 1)
            self.assertEqual(progress.uploads_to_run, 1)
            self.assertEqual(overview.pending_uploads, 1)
            self.assertEqual(overview.history_total, 1)
            self.assertEqual(overview.drafts, 1)
            self.assertEqual(overview.published, 0)
