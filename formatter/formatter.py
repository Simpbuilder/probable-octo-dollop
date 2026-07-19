"""Queue orchestration for converting downloaded clips into vertical ready files."""

from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path

from collector.models import ClipMetadata, FormatterConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .ffmpeg_client import FfmpegClientError, FfmpegClientProtocol
from .layout import calculate_fit_layout
from .models import FormatRequest, FormatSummary
from .utils import concise_error_message, ensure_path_is_within_directory, formatted_output_path


class PendingClipFormatter:
    """Format downloaded pending clips while keeping original media files untouched."""

    def __init__(
        self,
        metadata_file: Path,
        config: FormatterConfig,
        ffmpeg_client: FfmpegClientProtocol,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up metadata, layout settings, and an injectable FFmpeg adapter."""
        self._metadata_file = Path(metadata_file)
        self._config = config
        self._ffmpeg_client = ffmpeg_client
        self._logger = logger or logging.getLogger(__name__)

    def run(self) -> FormatSummary:
        """Format up to the configured number of downloaded clips awaiting processing."""
        summary = FormatSummary()
        self._ffmpeg_client.ensure_available()
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except Exception as error:
            summary.failed = 1
            self._logger.error("Could not load clip metadata for formatting: %s", error)
            return summary

        pending_clips = [
            clip
            for clip in clips
            if (
                clip.download_status == "downloaded"
                and clip.processing_status == "pending"
                and clip.local_file_path is not None
            )
        ]
        summary.pending = len(pending_clips)
        for clip in pending_clips[: self._config.maximum_clips_per_run]:
            self._process_clip(clip, summary)
        return summary

    def _process_clip(self, clip: ClipMetadata, summary: FormatSummary) -> None:
        """Render one clip or retain it as pending with an actionable error message."""
        try:
            existing_output = self._existing_output_file(clip)
            if existing_output is not None and not self._config.overwrite:
                self._mark_ready(clip, existing_output)
                summary.skipped += 1
                return

            input_file = self._resolve_input_file(clip.local_file_path)
            if not input_file.is_file():
                self._record_pending_issue(
                    clip, f"Downloaded input file does not exist: {input_file}"
                )
                summary.failed += 1
                return

            input_properties = self._ffmpeg_client.inspect(input_file)
            layout = calculate_fit_layout(input_properties, self._config)
            output_file = formatted_output_path(self._config.output_directory, clip.unique_id)
            self._config.output_directory.mkdir(parents=True, exist_ok=True)
            result = self._ffmpeg_client.format(
                FormatRequest(
                    input_file=input_file,
                    output_file=output_file,
                    input_properties=input_properties,
                    layout=layout,
                    config=self._config,
                )
            )
            self._mark_ready(clip, result.output_file)
            summary.formatted += 1
        except (FfmpegClientError, OSError, ValueError) as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.failed += 1
            self._logger.error("Formatting failed for %s: %s", clip.unique_id, error)
        except Exception as error:
            self._record_pending_issue(clip, concise_error_message(error))
            summary.failed += 1
            self._logger.exception("Unexpected formatting failure for %s", clip.unique_id)

    def _existing_output_file(self, clip: ClipMetadata) -> Path | None:
        """Return a saved ready path or stable expected target when either already exists."""
        candidates = []
        if clip.formatted_file_path is not None:
            formatted_path = Path(clip.formatted_file_path)
            if not formatted_path.is_absolute():
                formatted_path = self._config.output_directory / formatted_path.name
            candidates.append(formatted_path)
        candidates.append(formatted_output_path(self._config.output_directory, clip.unique_id))
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return ensure_path_is_within_directory(candidate, self._config.output_directory)
                except ValueError:
                    continue
        return None

    def _resolve_input_file(self, local_file_path: Path | None) -> Path:
        """Resolve stored absolute and legacy project-relative downloaded file paths."""
        if local_file_path is None:
            return Path()
        candidate = Path(local_file_path)
        if candidate.is_absolute():
            return candidate.resolve()

        project_root = self._metadata_file.parent.parent
        project_relative_candidate = project_root / candidate
        if project_relative_candidate.is_file():
            return project_relative_candidate.resolve()
        return (self._config.output_directory.parent / "pending" / candidate.name).resolve()

    def _mark_ready(self, clip: ClipMetadata, output_file: Path) -> None:
        """Persist a completed ready file without replacing original download metadata."""
        formatted_file_path = ensure_path_is_within_directory(
            output_file, self._config.output_directory
        )
        if not formatted_file_path.is_file():
            raise FfmpegClientError("Formatter reported success but the ready output file is missing.")
        update_clip_metadata(
            self._metadata_file,
            replace(
                clip,
                formatted_file_path=formatted_file_path,
                formatted_width=self._config.output_width,
                formatted_height=self._config.output_height,
                processing_status="ready",
                format_error=None,
            ),
        )

    def _record_pending_issue(self, clip: ClipMetadata, message: str) -> None:
        """Keep formatting failures retryable without altering the download state."""
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    processing_status="pending",
                    format_error=message,
                ),
            )
        except Exception as error:
            self._logger.error("Could not store the formatting error for %s: %s", clip.unique_id, error)
