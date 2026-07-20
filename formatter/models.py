"""Typed values shared by layout calculation, FFmpeg access, and queue formatting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from collector.models import FormatterConfig


@dataclass(frozen=True, slots=True)
class InputMediaProperties:
    """The video and audio details ffprobe reports for one source file."""

    width: int
    height: int
    has_audio: bool
    frame_rate: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid dimensions before layout calculations can run."""
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Input media dimensions must be greater than zero.")


@dataclass(frozen=True, slots=True)
class VideoLayout:
    """A fully visible source frame placed on a vertical output canvas."""

    canvas_width: int
    canvas_height: int
    video_width: int
    video_height: int
    x: int
    y: int
    video_area_width: int
    video_area_height: int


@dataclass(frozen=True, slots=True)
class FormatRequest:
    """All inputs needed to render one source clip into an MP4 output file."""

    input_file: Path
    output_file: Path
    input_properties: InputMediaProperties
    layout: VideoLayout
    config: FormatterConfig
    hook_overlay_file: Path | None = None


@dataclass(frozen=True, slots=True)
class FormatResult:
    """A confirmed output media file written by FFmpeg."""

    output_file: Path


@dataclass(slots=True)
class FormatSummary:
    """Counters displayed after one pending vertical-formatting pass."""

    pending: int = 0
    formatted: int = 0
    skipped: int = 0
    failed: int = 0
    eligible: int = 0
    processing: int = 0
    remaining: int = 0
