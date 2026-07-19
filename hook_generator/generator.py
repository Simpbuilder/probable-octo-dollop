"""Metadata-only OpenAI hook generation with strict local candidate validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import re
import unicodedata
from pathlib import Path

from collector.file_utils import concise_error_message
from collector.models import ClipMetadata, HookGenerationConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .client import HookGenerationClientProtocol


class HookGenerationResponseError(ValueError):
    """Raised when a model response is not exactly three usable hook candidates."""


@dataclass(slots=True)
class HookGenerationSummary:
    """Counters displayed after a metadata-only hook generation pass."""

    pending: int = 0
    generated: int = 0
    skipped: int = 0
    failed: int = 0


class PendingHookGenerator:
    """Generate candidate hooks without downloading or rendering media files."""

    def __init__(
        self,
        metadata_file: Path,
        config: HookGenerationConfig,
        client: HookGenerationClientProtocol,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up metadata storage, queue limits, and an injectable OpenAI adapter."""
        self._metadata_file = Path(metadata_file)
        self._config = config
        self._client = client
        self._logger = logger or logging.getLogger(__name__)

    def run(self, *, force: bool = False) -> HookGenerationSummary:
        """Generate candidates while keeping each failure retryable and isolated."""
        summary = HookGenerationSummary()
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except Exception as error:
            self._logger.error("Could not load clip metadata for hook generation: %s", error)
            summary.failed = 1
            return summary

        eligible_clips: list[ClipMetadata] = []
        for clip in clips:
            if clip.processing_status in {"rejected", "posted"}:
                continue
            if clip.hook_candidates and not force:
                summary.skipped += 1
                continue
            eligible_clips.append(clip)

        summary.pending = len(eligible_clips)
        for clip in eligible_clips[: self._config.maximum_clips_per_run]:
            self._generate_for_clip(clip, summary)
        return summary

    def _generate_for_clip(self, clip: ClipMetadata, summary: HookGenerationSummary) -> None:
        """Generate and persist one set of candidates without stopping later queue entries."""
        try:
            response_text = self._client.generate(
                model=self._config.model,
                instructions=_generation_instructions(self._config.maximum_characters),
                input_text=_generation_input(clip),
            )
            candidates = parse_hook_candidates(response_text, self._config.maximum_characters)
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    hook_candidates=candidates,
                    selected_hook=candidates[0] if self._config.automatic_selection else None,
                    hook_generation_status="generated",
                    hook_generation_error=None,
                    hook_generated_at=datetime.now(timezone.utc),
                    hook_model=self._config.model,
                ),
            )
            summary.generated += 1
        except Exception as error:
            message = concise_error_message(error)
            try:
                update_clip_metadata(
                    self._metadata_file,
                    replace(
                        clip,
                        hook_generation_status="failed",
                        hook_generation_error=message,
                    ),
                )
            except Exception as storage_error:
                self._logger.error(
                    "Could not store hook generation failure for %s: %s", clip.unique_id, storage_error
                )
            summary.failed += 1
            self._logger.error("Hook generation failed for %s: %s", clip.unique_id, error)


def parse_hook_candidates(response_text: str, maximum_characters: int) -> tuple[str, str, str]:
    """Parse a JSON-only response and enforce the local hook contract before storage."""
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise HookGenerationResponseError("OpenAI returned malformed hook JSON.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), list):
        raise HookGenerationResponseError("OpenAI response must contain a 'hooks' list.")
    raw_candidates = payload["hooks"]
    if len(raw_candidates) != 3:
        raise HookGenerationResponseError("OpenAI response must contain exactly three hooks.")

    candidates = tuple(
        _validate_hook_candidate(candidate, maximum_characters) for candidate in raw_candidates
    )
    canonical_candidates = {_canonical_candidate(candidate) for candidate in candidates}
    if len(canonical_candidates) != 3:
        raise HookGenerationResponseError("OpenAI hook candidates must be meaningfully distinct.")
    return candidates  # type: ignore[return-value]


def validate_custom_hook(text: str, maximum_characters: int) -> str:
    """Validate reviewer-entered text against the same short, clean local policy."""
    return _validate_hook_candidate(text, maximum_characters)


def _generation_instructions(maximum_characters: int) -> str:
    """Give the model narrow factual and formatting constraints for source-only hooks."""
    return (
        "Generate exactly three short English hook options for a public social video clip. "
        "Use curiosity naturally but do not invent facts or make unsupported claims. "
        "Base every option only on the supplied title and metadata. "
        "Each hook must use at most ten words and no more than "
        f"{maximum_characters} characters. Do not use hashtags, emojis, labels, numbering, "
        "or quotation marks. Make the three options meaningfully different. "
        'Return only valid JSON in this exact shape: {"hooks":["first","second","third"]}.'
    )


def _generation_input(clip: ClipMetadata) -> str:
    """Serialize only available source metadata; no video frames or media files are sent."""
    context = {
        "title": clip.title,
        "source": clip.source,
        "subreddit": clip.subreddit,
        "author": clip.author,
        "score": clip.score,
        "comment_count": clip.comment_count,
        "duration_seconds": clip.duration_seconds,
        "width": clip.width,
        "height": clip.height,
    }
    return json.dumps(context, ensure_ascii=True, sort_keys=True)


def _validate_hook_candidate(value: object, maximum_characters: int) -> str:
    """Normalize one candidate and reject unreviewable outputs locally."""
    if not isinstance(value, str):
        raise HookGenerationResponseError("Each hook candidate must be a string.")
    candidate = " ".join(value.split())
    if not candidate:
        raise HookGenerationResponseError("Hook candidates must not be empty.")
    if len(candidate) > maximum_characters:
        raise HookGenerationResponseError(
            f"Hook candidate exceeds the {maximum_characters}-character limit."
        )
    if len(candidate.split()) > 10:
        raise HookGenerationResponseError("Hook candidates must use at most ten words.")
    if "#" in candidate:
        raise HookGenerationResponseError("Hook candidates must not contain hashtags.")
    if _contains_emoji(candidate):
        raise HookGenerationResponseError("Hook candidates must not contain emojis.")
    return candidate


def _canonical_candidate(candidate: str) -> str:
    """Collapse punctuation and case so cosmetic variants cannot pass as distinct hooks."""
    return re.sub(r"[^a-z0-9]+", "", candidate.casefold())


def _contains_emoji(text: str) -> bool:
    """Reject common emoji and symbol code points while leaving ordinary punctuation intact."""
    return any(
        unicodedata.category(character) == "So"
        or "\U0001F300" <= character <= "\U0001FAFF"
        for character in text
    )
