"""Vertical Reel formatting support built around FFmpeg and ffprobe."""

from .ffmpeg_client import (
    FfmpegClient,
    FfmpegClientError,
    FfmpegDependencyError,
    FfmpegProcessError,
)
from .formatter import PendingClipFormatter
from .layout import calculate_fit_layout
from .models import (
    FormatRequest,
    FormatResult,
    FormatSummary,
    InputMediaProperties,
    VideoLayout,
)

__all__ = [
    "calculate_fit_layout",
    "FfmpegClient",
    "FfmpegClientError",
    "FfmpegDependencyError",
    "FfmpegProcessError",
    "FormatRequest",
    "FormatResult",
    "FormatSummary",
    "InputMediaProperties",
    "PendingClipFormatter",
    "VideoLayout",
]
