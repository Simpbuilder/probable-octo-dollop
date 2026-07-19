"""Interactive local review for saved OpenAI hook candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from collector.models import ClipMetadata, HookGenerationConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .generator import HookGenerationResponseError, validate_custom_hook


@dataclass(slots=True)
class HookReviewSummary:
    """Counters for one local candidate review session."""

    available: int = 0
    selected: int = 0
    custom: int = 0
    skipped: int = 0
    rejected: int = 0


class HookReviewer:
    """Select, customize, skip, or reject saved candidates without rendering media."""

    def __init__(self, metadata_file: Path, config: HookGenerationConfig) -> None:
        """Use the shared JSON store and configured custom-hook character limit."""
        self._metadata_file = Path(metadata_file)
        self._config = config

    def run(
        self,
        *,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
    ) -> HookReviewSummary:
        """Review unselected candidate sets and accept batch numeric choices when useful."""
        summary = HookReviewSummary()
        clips = [
            clip
            for clip in load_all_clip_metadata(self._metadata_file)
            if len(clip.hook_candidates) == 3 and clip.selected_hook is None
        ]
        summary.available = len(clips)
        index = 0
        while index < len(clips):
            clip = clips[index]
            self._show_clip(clip, output_func)
            choice = input_func("Select 1-3, c, s, r, or all 1-3: ").strip().lower()
            if choice in {"all 1", "all 2", "all 3"}:
                candidate_index = int(choice[-1]) - 1
                for remaining_clip in clips[index:]:
                    self._select_candidate(remaining_clip, candidate_index)
                    summary.selected += 1
                return summary
            if choice in {"1", "2", "3"}:
                self._select_candidate(clip, int(choice) - 1)
                summary.selected += 1
            elif choice == "c":
                custom_text = input_func("Custom hook: ")
                try:
                    custom_hook = validate_custom_hook(
                        custom_text, self._config.maximum_characters
                    )
                except HookGenerationResponseError as error:
                    output_func(f"Custom hook not saved: {error}")
                    continue
                self._save_custom_hook(clip, custom_hook)
                summary.custom += 1
            elif choice == "s":
                summary.skipped += 1
            elif choice == "r":
                update_clip_metadata(
                    self._metadata_file,
                    replace(
                        clip,
                        hook_candidates=(),
                        selected_hook=None,
                        hook_generation_status="rejected",
                        hook_generation_error=None,
                    ),
                )
                summary.rejected += 1
            else:
                output_func("Enter 1, 2, 3, c, s, r, or all 1, all 2, all 3.")
                continue
            index += 1
        return summary

    def _show_clip(self, clip: ClipMetadata, output_func: Callable[[str], None]) -> None:
        """Display only the metadata a reviewer needs to make a lightweight choice."""
        output_func(f"\nClip ID: {clip.unique_id}")
        output_func(f"Original title: {clip.title}")
        for index, candidate in enumerate(clip.hook_candidates, start=1):
            output_func(f"{index}. {candidate}")

    def _select_candidate(self, clip: ClipMetadata, candidate_index: int) -> None:
        """Persist one generated candidate as the later formatter selection."""
        update_clip_metadata(
            self._metadata_file,
            replace(
                clip,
                selected_hook=clip.hook_candidates[candidate_index],
                hook_generation_error=None,
            ),
        )

    def _save_custom_hook(self, clip: ClipMetadata, custom_hook: str) -> None:
        """Persist reviewer-entered hook text without discarding generated candidates."""
        update_clip_metadata(
            self._metadata_file,
            replace(
                clip,
                selected_hook=custom_hook,
                hook_generation_error=None,
            ),
        )
