"""Pure aspect-ratio-preserving layout calculation for vertical Reel canvases."""

from __future__ import annotations

from collector.models import FormatterConfig

from .models import InputMediaProperties, VideoLayout


def calculate_fit_layout(
    input_properties: InputMediaProperties,
    config: FormatterConfig,
) -> VideoLayout:
    """Fit the whole source frame into the configured area without cropping or stretching."""
    if config.crop_mode != "fit":
        raise ValueError(f"Unsupported crop mode: {config.crop_mode}")

    video_area_width = config.output_width - (2 * config.horizontal_margin)
    video_area_height = config.output_height - config.top_text_area_height - config.bottom_margin
    maximum_width = min(config.maximum_video_width, video_area_width)
    maximum_height = min(config.maximum_video_height, video_area_height)
    scale = min(
        maximum_width / input_properties.width,
        maximum_height / input_properties.height,
    )
    video_width = _even_dimension(input_properties.width * scale)
    video_height = _even_dimension(input_properties.height * scale)

    # Rounding for yuv420p can only reduce dimensions, so these values still fit.
    video_width = min(video_width, _even_dimension(maximum_width))
    video_height = min(video_height, _even_dimension(maximum_height))
    x = _even_position((config.output_width - video_width) // 2)
    y = _even_position(
        config.top_text_area_height + ((video_area_height - video_height) // 2)
    )
    return VideoLayout(
        canvas_width=config.output_width,
        canvas_height=config.output_height,
        video_width=video_width,
        video_height=video_height,
        x=x,
        y=y,
        video_area_width=video_area_width,
        video_area_height=video_area_height,
    )


def _even_dimension(value: float | int) -> int:
    """Round down to an FFmpeg-friendly even dimension without yielding zero."""
    return max(2, int(value) // 2 * 2)


def _even_position(value: int) -> int:
    """Align an overlay position to an even pixel for predictable chroma sampling."""
    return max(0, value // 2 * 2)
