"""Offline tests for conservative cleanup planning, execution, and metadata recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cleanup import execute_cleanup_plan, plan_cleanup, run_cleanup_command
from collector.models import ClipMetadata
from collector.storage import load_all_clip_metadata, save_clip_metadata
from publisher.history import append_post_history, build_post_history_record
from run_pipeline import main as run_pipeline_main


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def make_clip(
    unique_id: str,
    *,
    local_file: Path | None,
    formatted_file: Path | None = None,
    processing_status: str = "pending",
) -> ClipMetadata:
    """Create downloaded metadata suitable for cleanup reconciliation assertions."""
    return ClipMetadata(
        unique_id=unique_id,
        source="manual",
        subreddit=None,
        source_post_id=unique_id,
        source_url=f"https://example.invalid/{unique_id}",
        title=f"Clip {unique_id}",
        author="manual",
        score=0,
        comment_count=0,
        created_at=NOW,
        media_url=None,
        local_file_path=local_file,
        duration_seconds=20.0 if local_file is not None else None,
        width=720 if local_file is not None else None,
        height=1280 if local_file is not None else None,
        download_status="downloaded" if local_file is not None else "pending",
        processing_status=processing_status,  # type: ignore[arg-type]
        added_at=NOW,
        formatted_file_path=formatted_file,
        formatted_width=1080 if formatted_file is not None else None,
        formatted_height=1920 if formatted_file is not None else None,
    )


class CleanupTests(unittest.TestCase):
    """Verify cleanup never expands beyond its explicit local regeneration scope."""

    def make_environment(self):
        """Build an isolated pipeline layout containing protected and regeneratable artifacts."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        for relative in (
            "clips/pending",
            "clips/approved",
            "clips/rejected",
            "clips/ready/plain",
            "clips/ready/hooked",
            "clips/posted",
            "metadata",
            "logs",
            "config",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text("ZERNIO_API_KEY=private\n", encoding="utf-8")
        (root / "client_secret.json").write_text("{\"client_id\": \"private\"}\n", encoding="utf-8")
        (root / "token.json").write_text("{\"refresh_token\": \"private\"}\n", encoding="utf-8")
        (root / "config" / "collector.json").write_text("{}\n", encoding="utf-8")
        (root / "input_urls.txt").write_text("https://example.invalid/queued\n", encoding="utf-8")
        (root / "metadata" / "processed_urls.txt").write_text("https://example.invalid/done\n", encoding="utf-8")
        append_post_history(
            root / "metadata" / "zernio_post_history.json",
            build_post_history_record(
                post_id="post-1",
                status="published",
                account_id="account-1",
                filename="posted.mp4",
                public_media_url="https://media.example/posted.mp4",
                publish_mode="publish_now",
            ),
        )
        (root / "metadata" / "youtube_upload_history.json").write_text(
            '{"uploads": [{"youtube_video_id": "video-1", "status": "uploaded"}]}',
            encoding="utf-8",
        )
        (root / "clips" / "posted" / "posted.mp4").write_bytes(b"posted")
        return root

    def test_safe_cleanup_only_deletes_temporary_artifacts(self) -> None:
        """Safe cleanup preserves sources, metadata, configuration, history, posted media, and env."""
        root = self.make_environment()
        pending_file = root / "clips" / "pending" / "source.mp4"
        pending_file.write_bytes(b"source")
        empty_file = root / "clips" / "pending" / "empty.mp4"
        empty_file.write_bytes(b"")
        metadata_file = root / "metadata" / "clips.json"
        save_clip_metadata(metadata_file, make_clip("source", local_file=pending_file))
        save_clip_metadata(metadata_file, make_clip("empty", local_file=empty_file))
        temporary_files = (
            root / "clips" / "pending" / "source.mp4.part",
            root / "metadata" / "clips.json.tmp",
            root / "clips" / "hook-overlay.png",
        )
        for path in temporary_files:
            path.write_bytes(b"temporary")
        cache_directory = root / "collector" / "__pycache__"
        cache_directory.mkdir(parents=True)
        (cache_directory / "module.pyc").write_bytes(b"cache")

        result = execute_cleanup_plan(plan_cleanup(root))

        self.assertGreaterEqual(result.removed, len(temporary_files) + 2)
        self.assertTrue(pending_file.is_file())
        self.assertFalse(empty_file.exists())
        self.assertTrue((root / ".env").is_file())
        self.assertTrue((root / "client_secret.json").is_file())
        self.assertTrue((root / "token.json").is_file())
        self.assertTrue((root / "config" / "collector.json").is_file())
        self.assertTrue((root / "metadata" / "processed_urls.txt").is_file())
        self.assertTrue((root / "metadata" / "zernio_post_history.json").is_file())
        self.assertTrue((root / "metadata" / "youtube_upload_history.json").is_file())
        self.assertTrue((root / "clips" / "posted" / "posted.mp4").is_file())
        clips = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}
        self.assertEqual(clips["source"].download_status, "downloaded")
        self.assertEqual(clips["empty"].download_status, "pending")
        self.assertIsNone(clips["empty"].local_file_path)

    def test_dry_run_deletes_nothing(self) -> None:
        """A dry run prints a plan but leaves even safely removable files in place."""
        root = self.make_environment()
        temporary_file = root / "metadata" / "clips.json.tmp"
        temporary_file.write_text("temporary", encoding="utf-8")
        output: list[str] = []

        exit_code = run_cleanup_command(root, dry_run=True, output_func=output.append)

        self.assertEqual(exit_code, 0)
        self.assertTrue(temporary_file.is_file())
        self.assertIn("Dry run complete. No files were changed.", output)

    def test_all_temporary_preserves_history_and_resets_downloaded_and_ready_metadata(self) -> None:
        """Broad cleanup removes only regeneratable media and returns records to truthful retry states."""
        root = self.make_environment()
        pending_file = root / "clips" / "pending" / "pending.mp4"
        pending_file.write_bytes(b"pending")
        source_file = root / "clips" / "approved" / "source.mp4"
        source_file.write_bytes(b"source")
        ready_file = root / "clips" / "ready" / "hooked" / "ready.mp4"
        ready_file.write_bytes(b"ready")
        plain_file = root / "clips" / "ready" / "plain" / "plain.mp4"
        plain_file.write_bytes(b"plain")
        metadata_file = root / "metadata" / "clips.json"
        save_clip_metadata(metadata_file, make_clip("pending", local_file=pending_file))
        save_clip_metadata(
            metadata_file,
            make_clip(
                "ready",
                local_file=source_file,
                formatted_file=ready_file,
                processing_status="ready",
            ),
        )

        result = execute_cleanup_plan(plan_cleanup(root, all_temporary=True))

        self.assertEqual(result.errors, 0)
        self.assertFalse(pending_file.exists())
        self.assertFalse(ready_file.exists())
        self.assertFalse(plain_file.exists())
        self.assertTrue(source_file.is_file())
        self.assertTrue(metadata_file.is_file())
        self.assertTrue((root / "client_secret.json").is_file())
        self.assertTrue((root / "token.json").is_file())
        self.assertTrue((root / "metadata" / "zernio_post_history.json").is_file())
        self.assertTrue((root / "metadata" / "youtube_upload_history.json").is_file())
        self.assertTrue((root / "clips" / "posted" / "posted.mp4").is_file())
        clips = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}
        self.assertEqual(clips["pending"].download_status, "pending")
        self.assertIsNone(clips["pending"].local_file_path)
        self.assertEqual(clips["ready"].download_status, "downloaded")
        self.assertEqual(clips["ready"].processing_status, "pending")
        self.assertIsNone(clips["ready"].formatted_file_path)

    def test_project_reset_requires_exact_reset_and_preserves_env_config_history_and_posted_video(self) -> None:
        """A fresh-batch reset cannot proceed on yes/no text and never targets protected data."""
        root = self.make_environment()
        pending_file = root / "clips" / "pending" / "pending.mp4"
        pending_file.write_bytes(b"pending")
        metadata_file = root / "metadata" / "clips.json"
        save_clip_metadata(metadata_file, make_clip("pending", local_file=pending_file))
        output: list[str] = []

        canceled = run_cleanup_command(
            root,
            reset_project=True,
            input_func=lambda _: "yes",
            output_func=output.append,
        )
        self.assertEqual(canceled, 2)
        self.assertTrue(pending_file.is_file())
        self.assertTrue(metadata_file.is_file())

        completed = run_cleanup_command(
            root,
            reset_project=True,
            input_func=lambda _: "RESET",
            output_func=output.append,
        )
        self.assertEqual(completed, 0)
        self.assertFalse(pending_file.exists())
        self.assertFalse(metadata_file.exists())
        self.assertEqual((root / "input_urls.txt").read_text(encoding="utf-8"), "")
        self.assertTrue((root / ".env").is_file())
        self.assertTrue((root / "client_secret.json").is_file())
        self.assertTrue((root / "token.json").is_file())
        self.assertTrue((root / "config" / "collector.json").is_file())
        self.assertTrue((root / "metadata" / "zernio_post_history.json").is_file())
        self.assertTrue((root / "metadata" / "youtube_upload_history.json").is_file())
        self.assertTrue((root / "clips" / "posted" / "posted.mp4").is_file())

    def test_cleanup_cli_routes_to_the_shared_cleanup_command(self) -> None:
        """New cleanup flags are isolated from collection and delegate to the reusable cleanup runner."""
        with patch("run_pipeline.run_cleanup_command", return_value=0) as cleanup_command:
            self.assertEqual(run_pipeline_main(["--cleanup", "--all-temporary", "--yes"]), 0)

        self.assertTrue(cleanup_command.call_args.kwargs["all_temporary"])
        self.assertTrue(cleanup_command.call_args.kwargs["yes"])
        self.assertFalse(cleanup_command.call_args.kwargs["reset_project"])
