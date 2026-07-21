"""Offline tests for local persistent Streamlit background-job status."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from pipeline_runtime import BackgroundJobManager, QueueProgress, RuntimeStatus, RuntimeStatusStore
from ui_runtime import resolve_auto_refresh_interval


class PipelineRuntimeTests(unittest.TestCase):
    """Verify one local worker, atomic state writes, progress, and graceful cancellation."""

    def make_manager(self) -> tuple[TemporaryDirectory[str], RuntimeStatusStore, BackgroundJobManager]:
        """Create an isolated status store and worker manager for one test."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        store = RuntimeStatusStore(Path(temporary_directory.name) / "metadata" / "runtime_status.json")
        return temporary_directory, store, BackgroundJobManager(store)

    def wait_for_terminal(self, store: RuntimeStatusStore) -> RuntimeStatus:
        """Wait briefly for the deterministic local worker test runner to finish."""
        for _ in range(100):
            status = store.load()
            if not status.is_active:
                return status
            time.sleep(0.01)
        self.fail("background worker did not finish")

    def test_active_job_starts_once_and_reports_each_item(self) -> None:
        """Repeated Streamlit-style starts reuse the active worker instead of duplicating it."""
        _, store, manager = self.make_manager()
        release = threading.Event()
        starts: list[str] = []

        def runner(context) -> int:
            starts.append("started")
            self.assertTrue(context.report(QueueProgress("Download", "clip-1", 0, 2, 0, 2, "Starting.")))
            release.wait(timeout=1)
            self.assertTrue(context.report(QueueProgress("Download", "clip-1", 1, 2, 0, 1, "Downloaded.")))
            self.assertTrue(context.report(QueueProgress("Download", "clip-2", 2, 2, 0, 0, "Downloaded.")))
            return 0

        first = manager.start("Download", runner)
        second = manager.start("Download", runner)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(starts, ["started"])
        self.assertEqual(store.load().current_item, "clip-1")
        release.set()

        status = self.wait_for_terminal(store)
        self.assertEqual(status.status, "completed")
        self.assertEqual(status.completed_count, 2)

    def test_stop_request_prevents_later_items_and_preserves_completed_work(self) -> None:
        """A cancellation request is observed at the next item boundary without corrupting progress."""
        _, store, manager = self.make_manager()
        allow_second_boundary = threading.Event()
        processed: list[str] = []

        def runner(context) -> int:
            processed.append("clip-1")
            context.report(QueueProgress("Format", "clip-1", 1, 2, 0, 1, "Formatted."))
            allow_second_boundary.wait(timeout=1)
            if not context.report(QueueProgress("Format", "clip-2", 1, 2, 0, 1, "Starting.")):
                return 0
            processed.append("clip-2")
            return 0

        manager.start("Format", runner)
        manager.request_cancel()
        allow_second_boundary.set()

        status = self.wait_for_terminal(store)
        self.assertEqual(processed, ["clip-1"])
        self.assertEqual(status.status, "cancelled")
        self.assertEqual(status.completed_count, 1)

    def test_failed_job_and_malformed_file_recover_safely(self) -> None:
        """An exception is visible and malformed temporary JSON safely falls back to idle."""
        temporary_directory, store, manager = self.make_manager()

        def runner(context) -> int:
            raise RuntimeError("simulated failure")

        manager.start("Generate hooks", runner)
        status = self.wait_for_terminal(store)
        self.assertEqual(status.status, "failed")
        self.assertIn("simulated failure", status.last_message)

        store.path.write_text("not json", encoding="utf-8")
        self.assertEqual(store.load(), RuntimeStatus.idle())
        self.assertFalse(list((Path(temporary_directory.name) / "metadata").glob("*.tmp")))

    def test_atomic_write_and_refresh_validation(self) -> None:
        """Runtime writes leave no partial file and inactive pages do not request recurring polling."""
        _, store, _ = self.make_manager()
        written = store.write(RuntimeStatus(status="running", job_id="job", stage="Download"))

        self.assertEqual(store.load(), written)
        self.assertFalse(list(store.path.parent.glob("*.tmp")))
        self.assertEqual(resolve_auto_refresh_interval("1 second", written), 1)
        self.assertIsNone(resolve_auto_refresh_interval("Off", written))
        self.assertIsNone(resolve_auto_refresh_interval("2 seconds", RuntimeStatus.idle()))
        with self.assertRaises(ValueError):
            resolve_auto_refresh_interval("every second", written)
