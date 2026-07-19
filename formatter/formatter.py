"""Queue orchestration for converting downloaded clips into vertical ready files."""

from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from collector.models import ClipMetadata, FormatterConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .ffmpeg_client import FfmpegClientError, FfmpegClientProtocol
from .hooks import (
    HookRenderError,
    HookRenderResult,
    HookRendererProtocol,
    PillowHookRenderer,
    failed_hook_result,
    resolve_hook_selection,
    skipped_hook_result,
)
from .layout import calculate_fit_layout
from .models import FormatRequest, FormatResult, FormatSummary, InputMediaProperties, VideoLayout
from .utils import concise_error_message, ensure_path_is_within_directory, formatted_output_path


class PendingClipFormatter:
    """Format downloaded pending clips while keeping original media files untouched."""

    def __init__(
        self,
        metadata_file: Path,
        config: FormatterConfig,
        ffmpeg_client: FfmpegClientProtocol,
        *,
        hook_renderer: HookRendererProtocol | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up metadata, layout settings, and injectable media-rendering adapters."""
        self._metadata_file = Path(metadata_file)
        self._config = config
        self._ffmpeg_client = ffmpeg_client
        self._hook_renderer = hook_renderer or PillowHookRenderer()
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        manual_hook: str | None = None,
        include_ready_for_manual_hook: bool = False,
    ) -> FormatSummary:
        """Format eligible downloads, optionally validating one explicit manual hook override."""
        summary = FormatSummary()
        self._ffmpeg_client.ensure_available()
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except Exception as error:
            summary.failed = 1
            self._logger.error("Could not load clip metadata for formatting: %s", error)
            return summary

        eligible_clips = self._eligible_clips(
            clips,
            manual_hook=manual_hook,
            include_ready_for_manual_hook=include_ready_for_manual_hook,
        )
        summary.pending = len(eligible_clips)
        for clip in eligible_clips[: self._config.maximum_clips_per_run]:
            self._process_clip(clip, summary, manual_hook=manual_hook)
        return summary

    def _eligible_clips(
        self,
        clips: list[ClipMetadata],
        *,
        manual_hook: str | None,
        include_ready_for_manual_hook: bool,
    ) -> list[ClipMetadata]:
        """Keep normal runs pending-only while allowing one explicit hook validation on ready media."""
        downloaded_clips = [
            clip
            for clip in clips
            if clip.download_status == "downloaded" and clip.local_file_path is not None
        ]
        pending_clips = [
            clip for clip in downloaded_clips if clip.processing_status == "pending"
        ]
        if manual_hook is None or not include_ready_for_manual_hook or pending_clips:
            return pending_clips
        return [clip for clip in downloaded_clips if clip.processing_status == "ready"]

    def _process_clip(
        self,
        clip: ClipMetadata,
        summary: FormatSummary,
        *,
        manual_hook: str | None,
    ) -> None:
        """Render one clip or retain it as pending with an actionable error message."""
        selection = resolve_hook_selection(clip, self._config.hook, manual_hook)
        hook_result = skipped_hook_result(clip)
        hook_attempted = False
        try:
            output_file = formatted_output_path(
                self._config.output_directory,
                clip.unique_id,
                selection.text if selection is not None else None,
            )
            existing_output = self._existing_output_file(output_file)
            if existing_output is not None and not self._config.overwrite:
                if selection is not None:
                    hook_result = HookRenderResult(
                        overlay_file=None,
                        text=selection.text,
                        source=selection.source,
                        status="rendered",
                    )
                self._mark_ready(clip, existing_output, hook_result)
                summary.skipped += 1
                return

            input_file = self._resolve_input_file(clip.local_file_path)
            if not input_file.is_file():
                self._record_pending_issue(
                    clip,
                    f"Downloaded input file does not exist: {input_file}",
                    hook_result,
                )
                summary.failed += 1
                return

            input_properties = self._ffmpeg_client.inspect(input_file)
            layout = calculate_fit_layout(input_properties, self._config)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            if selection is not None:
                hook_attempted = True
                with TemporaryDirectory(prefix="viral-clip-hook-") as temporary_directory:
                    hook_result = self._hook_renderer.render(
                        selection,
                        self._config.hook,
                        canvas_width=self._config.output_width,
                        canvas_height=self._config.output_height,
                        overlay_file=Path(temporary_directory) / "hook-overlay.png",
                    )
                    if hook_result.status != "rendered" or hook_result.overlay_file is None:
                        raise HookRenderError("Hook renderer did not return a usable overlay image.")
                    if hook_result.used_font_fallback:
                        self._logger.warning(
                            "Hook font fallback for %s: %s",
                            clip.unique_id,
                            hook_result.font_fallback_message
                            or hook_result.font_path
                            or "Pillow built-in font",
                        )
                    result = self._render_video(
                        input_file,
                        output_file,
                        input_properties,
                        layout,
                        hook_result.overlay_file,
                    )
            else:
                result = self._render_video(
                    input_file,
                    output_file,
                    input_properties,
                    layout,
                    hook_overlay_file=None,
                )
            self._mark_ready(clip, result.output_file, hook_result)
            summary.formatted += 1
        except (FfmpegClientError, HookRenderError, OSError, ValueError) as error:
            message = concise_error_message(error)
            if hook_attempted and selection is not None:
                hook_result = failed_hook_result(selection, message)
            self._record_pending_issue(clip, message, hook_result)
            summary.failed += 1
            self._logger.error("Formatting failed for %s: %s", clip.unique_id, error)
        except Exception as error:
            message = concise_error_message(error)
            if hook_attempted and selection is not None:
                hook_result = failed_hook_result(selection, message)
            self._record_pending_issue(clip, message, hook_result)
            summary.failed += 1
            self._logger.exception("Unexpected formatting failure for %s", clip.unique_id)

    def _render_video(
        self,
        input_file: Path,
        output_file: Path,
        input_properties: InputMediaProperties,
        layout: VideoLayout,
        hook_overlay_file: Path | None,
    ) -> FormatResult:
        """Delegate video composition while preserving a narrow queue-orchestration surface."""
        return self._ffmpeg_client.format(
            FormatRequest(
                input_file=input_file,
                output_file=output_file,
                input_properties=input_properties,
                layout=layout,
                config=self._config,
                hook_overlay_file=hook_overlay_file,
            )
        )

    def _existing_output_file(self, output_file: Path) -> Path | None:
        """Return a routed target or a same-name root-level legacy output without moving it."""
        legacy_output_file = self._config.output_directory / output_file.name
        for candidate in (output_file, legacy_output_file):
            if not candidate.is_file():
                continue
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

    def _mark_ready(
        self,
        clip: ClipMetadata,
        output_file: Path,
        hook_result: HookRenderResult,
    ) -> None:
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
                hook_text=hook_result.text,
                hook_status=hook_result.status,
                hook_source=hook_result.source,
                hook_error=hook_result.error,
            ),
        )

    def _record_pending_issue(
        self,
        clip: ClipMetadata,
        message: str,
        hook_result: HookRenderResult,
    ) -> None:
        """Keep formatting failures retryable without altering the download state."""
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    processing_status="pending",
                    format_error=message,
                    hook_text=hook_result.text,
                    hook_status=hook_result.status,
                    hook_source=hook_result.source,
                    hook_error=hook_result.error,
                ),
            )
        except Exception as error:
            self._logger.error("Could not store the formatting error for %s: %s", clip.unique_id, error)
