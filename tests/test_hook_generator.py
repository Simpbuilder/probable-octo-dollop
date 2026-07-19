"""Offline tests for OpenAI hook candidate generation and local review selection."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collector.config import load_collector_config
from collector.models import ClipMetadata, HookConfig, HookGenerationConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from hook_generator.client import HookGenerationClientError, HookGenerationCredentialsError, load_openai_api_key
from hook_generator.diagnostics import inspect_hook_flow
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
                candidate_response("That was wild", "That was wild!", "Then he looked"),
                60,
            )
        with self.assertRaises(HookGenerationResponseError):
            parse_hook_candidates(
                candidate_response("x" * 61, "Then he looked", "Nobody saw this"),
                60,
            )
        with self.assertRaises(HookGenerationResponseError):
            parse_hook_candidates(
                candidate_response(
                    "One two three four five six seven eight nine ten",
                    "Then he looked",
                    "Nobody saw this",
                ),
                60,
            )

    def test_blocked_generic_candidates_are_regenerated(self) -> None:
        """A template-like result triggers one clean retry before the clip is marked failed."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, make_clip())
            client = FakeHookClient(
                [
                    candidate_response(
                        "Prepare for a surprise",
                        "Wait for the ending",
                        "What happens next?",
                    ),
                    candidate_response(
                        "He said WHAT?",
                        "That was unexpected",
                        "This went wrong fast",
                    ),
                ]
            )

            summary = self.make_generator(metadata_file, client).run()

            updated_clip = load_all_clip_metadata(metadata_file)[0]
            self.assertEqual(summary.generated, 1)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(
                updated_clip.hook_candidates,
                ("He said WHAT?", "That was unexpected", "This went wrong fast"),
            )
            self.assertIn("Previous hook set was rejected", client.calls[1]["input_text"])

    def test_numbered_bulleted_and_quoted_candidates_are_cleaned(self) -> None:
        """Model list decoration is removed without changing natural caption punctuation."""
        candidates = parse_hook_candidates(
            candidate_response(
                '1. "He said WHAT?"',
                "- 'This went wrong fast'",
                "3) The timing was perfect",
            ),
            60,
        )

        self.assertEqual(
            candidates,
            ("He said WHAT?", "This went wrong fast", "The timing was perfect"),
        )

    def test_concise_casual_candidates_preserve_mixed_punctuation(self) -> None:
        """Short casual options retain apostrophes, ellipses, and a single natural reaction mark."""
        candidates = parse_hook_candidates(
            candidate_response(
                "He said WHAT?",
                "I couldn't believe it...",
                "This was not the plan",
            ),
            60,
        )

        self.assertEqual(candidates[0], "He said WHAT?")
        self.assertEqual(candidates[1], "I couldn't believe it...")
        self.assertEqual(candidates[2], "This was not the plan")
        self.assertTrue(all(2 <= len(candidate.split()) <= 7 for candidate in candidates))
        self.assertEqual(len({candidate.casefold() for candidate in candidates}), 3)

    def test_generation_prompt_requests_three_distinct_casual_styles(self) -> None:
        """The API prompt explicitly separates reaction, commentary, and sarcastic styles."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, make_clip())
            client = FakeHookClient(
                [candidate_response("Bro had one job", "The timing was perfect", "He really tried that")]
            )

            self.make_generator(metadata_file, client).run()

            instructions = client.calls[0]["instructions"]
            self.assertIn("three noticeably different styles", instructions)
            self.assertIn("two to seven words", instructions)
            self.assertIn("hard maximum of nine words", instructions)

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
            first_clip = replace(
                self.make_reviewable_clip("review-first"),
                hook_candidates=("He said WHAT?", "I couldn't believe it...", "This was not the plan"),
            )
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
            self.assertEqual(
                clips_by_id[first_clip.unique_id].selected_hook,
                "I couldn't believe it...",
            )
            self.assertEqual(
                clips_by_id[first_clip.unique_id].hook_candidates,
                ("He said WHAT?", "I couldn't believe it...", "This was not the plan"),
            )
            self.assertEqual(
                clips_by_id[second_clip.unique_id].selected_hook,
                "A custom reaction hook",
            )
            self.assertTrue(any("Clip ID: review-first" in line for line in output))
            self.assertTrue(any(f"Metadata file: {metadata_file.resolve()}" in line for line in output))
            self.assertIn("1. He said WHAT?", output)
            self.assertIn("2. I couldn't believe it...", output)
            self.assertIn("3. This was not the plan", output)

    def test_review_never_invokes_hook_generation(self) -> None:
        """Review reads saved candidates only and never constructs a generation request."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            save_clip_metadata(metadata_file, self.make_reviewable_clip("review-no-generation"))

            with patch("hook_generator.generator.PendingHookGenerator.run") as generate:
                HookReviewer(
                    metadata_file,
                    HookGenerationConfig(maximum_characters=60),
                ).run(input_func=lambda _: "s", output_func=lambda _: None)

            generate.assert_not_called()


class HookReviewContinuationTests(unittest.TestCase):
    """Keep remaining review choices covered without involving generation or rendering."""

    def make_reviewable_clip(self, unique_id: str) -> ClipMetadata:
        """Create a generated candidate set ready for a reviewer decision."""
        return replace(
            make_clip(unique_id),
            hook_candidates=("First option", "Second option", "Third option"),
            hook_generation_status="generated",
            hook_model="test-model",
            hook_generated_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        )

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

    def test_format_run_never_starts_hook_generation(self) -> None:
        """Formatting is isolated even when automatic hook generation is enabled in config."""
        project_root = Path(__file__).resolve().parents[1]
        config = load_collector_config(project_root / "config")
        enabled_config = replace(
            config,
            hook_generation_config=replace(config.hook_generation_config, enabled=True),
            formatter_config=replace(config.formatter_config, enabled=True),
        )

        with (
            patch("run_pipeline.load_collector_config", return_value=enabled_config),
            patch("run_pipeline.run_pending_hook_generator", return_value=0) as generator,
            patch("run_pipeline.run_pending_clip_formatter", return_value=0) as formatter,
        ):
            self.assertEqual(run_pipeline_main(["--format"]), 0)

        generator.assert_not_called()
        formatter.assert_called_once()

    def test_debug_hook_flow_prints_exact_configured_record_without_generation(self) -> None:
        """The diagnostic exposes one saved clip's values without running a pipeline stage."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "metadata" / "clips.json"
            clip = replace(
                make_clip("debug-clip"),
                hook_candidates=("First saved hook", "Second saved hook", "Third saved hook"),
                selected_hook="Second saved hook",
            )
            save_clip_metadata(metadata_file, clip)
            project_root = Path(__file__).resolve().parents[1]
            config = load_collector_config(project_root / "config")
            debug_config = replace(
                config,
                metadata_file=metadata_file,
                formatter_config=replace(config.formatter_config, hook=HookConfig(enabled=True)),
            )
            output = StringIO()

            with (
                patch("run_pipeline.load_collector_config", return_value=debug_config),
                patch("run_pipeline.run_pending_hook_generator", return_value=0) as generator,
                redirect_stdout(output),
            ):
                self.assertEqual(run_pipeline_main(["--debug-hook-flow", "debug-clip"]), 0)

            generator.assert_not_called()
            rendered_output = output.getvalue()
            self.assertIn(f"Metadata file: {metadata_file.resolve()}", rendered_output)
            self.assertIn("Clip ID: debug-clip", rendered_output)
            self.assertIn("1. First saved hook", rendered_output)
            self.assertIn("Selected hook: Second saved hook", rendered_output)
            self.assertIn("Final hook for rendering: Second saved hook", rendered_output)
            self.assertIn("Reason/source: selected_hook (generated)", rendered_output)


class HookFlowDiagnosticsTests(unittest.TestCase):
    """Verify diagnostics use one exact metadata store and the shared formatter resolver."""

    def test_diagnostic_does_not_load_stale_or_unrelated_metadata(self) -> None:
        """A same-ID record in another JSON file cannot affect the inspected clip flow."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            configured_metadata = root / "metadata" / "clips.json"
            stale_metadata = root / "old-metadata" / "clips.json"
            configured_clip = replace(
                make_clip("same-id"),
                hook_candidates=("Configured first", "Configured second", "Configured third"),
                selected_hook="Configured second",
            )
            stale_clip = replace(
                make_clip("same-id"),
                hook_candidates=("Stale first", "Stale second", "Stale third"),
                selected_hook="Stale second",
            )
            save_clip_metadata(configured_metadata, configured_clip)
            save_clip_metadata(stale_metadata, stale_clip)

            debug = inspect_hook_flow(configured_metadata, "same-id", HookConfig(enabled=True))

            self.assertEqual(debug.metadata_file, configured_metadata.resolve())
            self.assertEqual(debug.hook_candidates, configured_clip.hook_candidates)
            self.assertEqual(debug.selected_hook, "Configured second")
            self.assertIsNotNone(debug.selection)
            self.assertEqual(debug.selection.text, "Configured second")
            self.assertEqual(debug.selection.reason, "selected_hook")
