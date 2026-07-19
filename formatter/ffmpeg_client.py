"""Safe FFmpeg and ffprobe subprocess access for vertical clip rendering."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
import shutil
import subprocess
from typing import Any, Protocol

from .models import FormatRequest, FormatResult, InputMediaProperties
from .utils import concise_error_message


class FfmpegClientError(RuntimeError):
    """Base error raised for an inspection or rendering problem."""


class FfmpegDependencyError(FfmpegClientError):
    """Raised when FFmpeg or ffprobe cannot be found on PATH."""


class FfmpegProcessError(FfmpegClientError):
    """Raised when FFmpeg or ffprobe exits without a usable result."""


class FfmpegClientProtocol(Protocol):
    """The small FFmpeg surface required by pending-clip formatting."""

    def ensure_available(self) -> None:
        """Raise a focused error when FFmpeg or ffprobe is unavailable."""

    def inspect(self, input_file: Path) -> InputMediaProperties:
        """Inspect one local media file without changing it."""

    def format(self, request: FormatRequest) -> FormatResult:
        """Render one formatted vertical MP4 file."""


class FfmpegClient:
    """Build Windows-safe argument lists and invoke FFmpeg without a shell."""

    def __init__(
        self,
        *,
        ffmpeg_executable: str | None = None,
        ffprobe_executable: str | None = None,
    ) -> None:
        """Use explicit executable paths in tests or discover both tools on PATH."""
        self._ffmpeg_executable = ffmpeg_executable or shutil.which("ffmpeg")
        self._ffprobe_executable = ffprobe_executable or shutil.which("ffprobe")

    def ensure_available(self) -> None:
        """Confirm both FFmpeg tools are available before any source is processed."""
        missing = []
        if self._ffmpeg_executable is None:
            missing.append("ffmpeg")
        if self._ffprobe_executable is None:
            missing.append("ffprobe")
        if missing:
            raise FfmpegDependencyError(
                f"Missing required executable(s) on PATH: {', '.join(missing)}. "
                "Install FFmpeg and make both ffmpeg and ffprobe available."
            )

    def inspect(self, input_file: Path) -> InputMediaProperties:
        """Use ffprobe JSON output to read source dimensions and audio availability."""
        self.ensure_available()
        input_file = Path(input_file)
        if not input_file.is_file():
            raise FfmpegProcessError(f"Input video file does not exist: {input_file}")

        command = self.build_probe_command(input_file)
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise FfmpegProcessError(_process_error_message("ffprobe", completed))
        return _parse_probe_output(completed.stdout)

    def format(self, request: FormatRequest) -> FormatResult:
        """Render a playable H.264/AAC MP4 with optional source audio mapping."""
        self.ensure_available()
        if not request.input_file.is_file():
            raise FfmpegProcessError(f"Input video file does not exist: {request.input_file}")
        if request.hook_overlay_file is not None and not request.hook_overlay_file.is_file():
            raise FfmpegProcessError(
                f"Hook overlay image does not exist: {request.hook_overlay_file}"
            )
        request.output_file.parent.mkdir(parents=True, exist_ok=True)

        command = self.build_format_command(request)
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise FfmpegProcessError(_process_error_message("FFmpeg", completed))
        if not request.output_file.is_file():
            raise FfmpegProcessError("FFmpeg completed without creating the formatted output file.")
        return FormatResult(output_file=request.output_file.resolve())

    def build_probe_command(self, input_file: Path) -> list[str]:
        """Build a list-form ffprobe command that works without shell quoting."""
        self.ensure_available()
        return [
            self._ffprobe_executable,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(input_file),
        ]

    def build_format_command(self, request: FormatRequest) -> list[str]:
        """Build a no-crop H.264/AAC FFmpeg command as a Windows-safe argument list."""
        self.ensure_available()
        config = request.config
        layout = request.layout
        filters = [
            (
                f"color=c={config.background_color}:s={layout.canvas_width}x"
                f"{layout.canvas_height}:r={config.output_frame_rate}[canvas]"
            ),
            (
                f"[0:v]scale={layout.video_width}:{layout.video_height}:flags=lanczos,"
                f"setsar=1,fps={config.output_frame_rate}[source]"
            ),
        ]
        if request.hook_overlay_file is None:
            filters.append(
                f"[canvas][source]overlay=x={layout.x}:y={layout.y}:shortest=1:format=auto[video]"
            )
        else:
            filters.extend(
                (
                    f"[canvas][source]overlay=x={layout.x}:y={layout.y}:shortest=1:format=auto[base]",
                    "[1:v]format=rgba,setpts=PTS-STARTPTS[hook]",
                    "[base][hook]overlay=x=0:y=0:shortest=1:format=auto[video]",
                )
            )
        command = [
            self._ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y" if config.overwrite else "-n",
            "-i",
            str(request.input_file),
        ]
        if request.hook_overlay_file is not None:
            command.extend(
                (
                    "-loop",
                    "1",
                    "-framerate",
                    str(config.output_frame_rate),
                    "-i",
                    str(request.hook_overlay_file),
                )
            )
        command.extend(
            (
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[video]",
                "-map",
                "0:a?",
                "-c:v",
                config.video_codec,
                "-preset",
                config.encoding_preset,
                "-crf",
                str(config.crf),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(config.output_frame_rate),
                "-c:a",
                config.audio_codec,
                "-movflags",
                "+faststart",
                "-shortest",
                str(request.output_file),
            )
        )
        return command


def _parse_probe_output(output: str) -> InputMediaProperties:
    """Validate ffprobe JSON without accepting malformed stream metadata."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise FfmpegProcessError("ffprobe returned invalid JSON.") from error
    if not isinstance(data, Mapping):
        raise FfmpegProcessError("ffprobe returned an invalid media description.")
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise FfmpegProcessError("ffprobe did not report media streams.")

    video_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if video_stream is None:
        raise FfmpegProcessError("Source media does not contain a video stream.")
    width = _required_positive_int(video_stream, "width")
    height = _required_positive_int(video_stream, "height")
    frame_rate = _optional_string(video_stream.get("avg_frame_rate")) or _optional_string(
        video_stream.get("r_frame_rate")
    )
    has_audio = any(
        isinstance(stream, Mapping) and stream.get("codec_type") == "audio" for stream in streams
    )
    return InputMediaProperties(
        width=width,
        height=height,
        has_audio=has_audio,
        frame_rate=frame_rate,
    )


def _required_positive_int(data: Mapping[str, Any], field_name: str) -> int:
    """Return one required positive integer from ffprobe's stream description."""
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FfmpegProcessError(f"ffprobe reported no valid {field_name} for the video stream.")
    return value


def _optional_string(value: object) -> str | None:
    """Normalize an optional non-empty ffprobe string value."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _process_error_message(tool_name: str, completed: subprocess.CompletedProcess[str]) -> str:
    """Return a bounded tool error that is safe to persist in clip metadata."""
    detail = completed.stderr or completed.stdout or f"{tool_name} exited with {completed.returncode}."
    return f"{tool_name} failed: {concise_error_message(detail)}"
