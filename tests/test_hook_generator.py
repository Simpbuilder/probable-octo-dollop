"""Offline tests for OpenAI hook candidate generation and local review selection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collector.models import ClipMetadata, HookGenerationConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from hook_generator.client import HookGenerationClientError, HookGenerationCredentialsError, load_openai_api_key
from hook_generator.generator import (
    HookGenerationResponseError,
    PendingHookGenerator,
    parse_hook_candidates,
)
from hook_generator.review import HookReviewer
from run_pipeline import main as run_pipeline_main


def make_clip(unique_id: str = "hook-generator") -> ClipMetadata:
    """Create deterministic source metadata without requiring downloaded media."""
    timestamp = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    return ClipMetadata(
        unique_id=unique_id,
        source="reddit",
        subreddit="funny",
        source_post_id=unique_id,
        source_url=f"https://www.reddit.com/r/funny/comments/{unique_id}",
        title="He thought nobody was watching",
        author="test_author",
        score=750,
        comment_count=42,
        created_at=timestamp,
        media_url=None,
        local_file_path=None,
        added_at=timestamp,
    )


def candidate_response(*candidates: str) -> str:
    """Return the JSON-only payload expected from the OpenAI adapter."""
    return json.dumps({"hooks": list(candidates)})


class FakeHookClient:
    """Return scripted API outcomes without importing the OpenAI SDK or using a network."""

    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    def generate(self, *, model: str, instructions: str, input_text: str) -> str:
        """Record the prompt and return one scripted response or raise its scripted failure."""
        self.calls.append({"model": model, "instructions": instructions, "input_text": input_text})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class HookGenerationTests(unittest.TestCase):
    """Verify generation validates model output and leaves later work independent."""

    def make_generator(
        self,
        metadata_file: Path,
        client: FakeHookClient,
        **config_overrides: object,
    ) -> PendingHookGenerator:
        """Build a quiet generator with small, deterministic queue settings."""
        config_values: dict[str, object] = {
            "model": "test-model",
            "maximum_characters": 60,
            "maximum_clips_per_run": 5,
        }
        config_values.update(config_overrides)
        logger = logging.getLogger(f"test_hook_generator_{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return PendingHookGenerator(
            metadata_file,
            HookGenerationConfig(**config_values),  # type: ignore[arg-type]
            client,
            logger=logger,
        )

    def test_successful_generation_saves_exactly_three_candidates(self) -> None:
        """A valid mocked response records candidates, model, timestamp, and generated status."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            clip = make_clip()
            save_clip_metadata(metadata_file, clip)
            client = FakeHookClient(
                [candidate_response("Nobody expected this", "Then he looked up", "The timing was perfect")]
            )

            summary = self.make_generator(metadata_file, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.generated, 1)
            self.assertEqual(updated_clip.hook_candidates, (
                "Nobody expected this",
                "Then he looked up",
                "The timing was perfect",
            ))
            self.assertIsNone(updated_clip.selected_hook)
            self.assertEqual(updated_clip.hook_generation_status, "generated")
            self.assertIsNone(updated_clip.hook_generation_error)
            self.assertIsNotNone(updated_clip.hook_generated_at)
            self.assertEqual(updated_clip.hook_model, "test-model")
            self.assertIn('"title": "He thought nobody was watching"', client.calls[0]["input_text"])

    def test_missing_api_key_is_reported_without_using_environment_state(self) -> None:
        """An absent key produces a focused setup error before a client is constructed."""
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaises(HookGenerationCredentialsError):
                load_openai_api_key(Path(temporary_directory) / ".env", environ={})

    def test_malformed_response_is_retryable(self) -> None:
        """Non-JSON model output leaves the clip unselected and records a failed generation."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, make_clip())

            summary = self.make_generator(metadata_file, FakeHookClient(["not json"])).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.failed, 1)
            self.assertEqual(updated_clip.hook_generation_status, "failed")
            self.assertIn("malformed", updated_clip.hook_generation_error)
            self.assertEqual(updated_clip.hook_candidates, ())

    def test_duplicate_and_overlong_candidates_are_rejected(self) -> None:
        """Cosmetic duplicates and character-limit violations are rejected before metadata writes."""
        with self.assertRaises(HookGenerationResponseError):
            parse_hook_candidates(
                candidate_response("Wait for it", "Wait for it!", "Then he looked"),
                60,
            )
        with self.assertRaises(HookGenerationResponseError):
            parse_hook_candidates(
                candidate_response("x" * 61, "Then he looked", "Nobody saw this"),
                60,
            )

    def test_api_failure_does_not_stop_later_clips(self) -> None:
        """A failed API call stores an error while a later clip can still generate candidates."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            first_clip = make_clip("hook-failure")
            second_clip = make_clip("hook-success")
            save_clip_metadata(metadata_file, first_clip)
            save_clip_metadata(metadata_file, second_clip)
            client = FakeHookClient([
                HookGenerationClientError("network unavailable"),
                candidate_response("Nobody saw that", "Then it happened", "Watch his reaction"),
            ])

            summary = self.make_generator(metadata_file, client).run()

            clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.generated, 1)
            self.assertEqual(clips_by_id[first_clip.unique_id].hook_generation_status, "failed")
            self.assertEqual(clips_by_id[second_clip.unique_id].hook_generation_status, "generated")

    def test_existing_candidates_are_skipped_unless_force_is_used(self) -> None:
        """Saved candidates avoid duplicate API work until an explicit regeneration is requested."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            existing_clip = replace(
                make_clip(),
                hook_candidates=("Old first hook", "Old second hook", "Old third hook"),
                hook_generation_status="generated",
            )
            save_clip_metadata(metadata_file, existing_clip)
            client = FakeHookClient(
                [candidate_response("New first hook", "New second hook", "New third hook")]
            )
            generator = self.make_generator(metadata_file, client)

            skipped_summary = generator.run()
            forced_summary = generator.run(force=True)

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(skipped_summary.skipped, 1)
            self.assertEqual(skipped_summary.generated, 0)
            self.assertEqual(forced_summary.generated, 1)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(updated_clip.hook_candidates[0], "New first hook")

    def test_automatic_selection_uses_first_generated_candidate_only_when_enabled(self) -> None:
        """The disabled-by-default setting keeps review manual while supporting explicit automation."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, make_clip())
            client = FakeHookClient(
                [candidate_response("First choice", "Second choice", "Third choice")]
            )

            self.make_generator(metadata_file, client, automatic_selection=True).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(updated_clip.selected_hook, "First choice")


class HookReviewTests(unittest.TestCase):
    """Verify local numeric and custom review choices persist without a generation call."""

    def make_reviewable_clip(self, unique_id: str) -> ClipMetadata:
        """Create a generated candidate set ready for a reviewer decision."""
        return replace(
            make_clip(unique_id),
            hook_candidates=("First option", "Second option", "Third option"),
            hook_generation_status="generated",
            hook_model="test-model",
            hook_generated_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        )

    def test_selection_and_custom_hook_saving(self) -> None:
        """Numeric and custom review choices become selected_hook metadata without rendering media."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            first_clip = self.make_reviewable_clip("review-first")
            second_clip = self.make_reviewable_clip("review-second")
            save_clip_metadata(metadata_file, first_clip)
            save_clip_metadata(metadata_file, second_clip)
            answers = iter(["2", "c", "A custom reaction hook"])
            output: list[str] = []

            summary = HookReviewer(
                metadata_file,
                HookGenerationConfig(maximum_characters=60),
            ).run(input_func=lambda _: next(answers), output_func=output.append)

            clips_by_id = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}
            self.assertEqual(summary.selected, 1)
            self.assertEqual(summary.custom, 1)
            self.assertEqual(clips_by_id[first_clip.unique_id].selected_hook, "Second option")
            self.assertEqual(
                clips_by_id[second_clip.unique_id].selected_hook,
                "A custom reaction hook",
            )
            self.assertTrue(any("Clip ID: review-first" in line for line in output))

    def test_all_selection_chooses_one_candidate_for_remaining_clips(self) -> None:
        """A practical batch command applies the requested numeric option to all shown clips."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, self.make_reviewable_clip("review-all-first"))
            save_clip_metadata(metadata_file, self.make_reviewable_clip("review-all-second"))

            summary = HookReviewer(
                metadata_file,
                HookGenerationConfig(maximum_characters=60),
            ).run(input_func=lambda _: "all 1", output_func=lambda _: None)

            self.assertEqual(summary.selected, 2)
            self.assertTrue(
                all(clip.selected_hook == "First option" for clip in load_all_clip_metadata(metadata_file))
            )

    def test_reject_all_clears_candidates_for_a_later_regeneration(self) -> None:
        """Rejecting a candidate set keeps the clip unselected and records the reviewer decision."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, self.make_reviewable_clip("review-reject"))

            summary = HookReviewer(
                metadata_file,
                HookGenerationConfig(maximum_characters=60),
            ).run(input_func=lambda _: "r", output_func=lambda _: None)

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.rejected, 1)
            self.assertEqual(updated_clip.hook_candidates, ())
            self.assertIsNone(updated_clip.selected_hook)
            self.assertEqual(updated_clip.hook_generation_status, "rejected")


class HookGenerationRunnerTests(unittest.TestCase):
    """Verify the explicit generation command does not enter other pipeline stages."""

    def test_generate_hooks_flag_runs_only_generation(self) -> None:
        """The CLI returns after the hook queue function and does not collect, download, or format."""
        with patch("run_pipeline.run_pending_hook_generator", return_value=0) as generator:
            self.assertEqual(run_pipeline_main(["--generate-hooks"]), 0)

        generator.assert_called_once()

    def test_generate_hooks_cannot_be_combined_with_formatting(self) -> None:
        """A focused generation run rejects flags that would start a media stage."""
        self.assertEqual(run_pipeline_main(["--generate-hooks", "--format"]), 2)
