"""Word-safe hook text wrapping and size selection independent of image rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from collector.models import HookConfig


class FontMetrics(Protocol):
    """The small Pillow font surface needed for deterministic hook layout."""

    def getlength(self, text: str) -> float:
        """Return the rendered advance width for ``text``."""

    def getbbox(self, text: str) -> tuple[int, int, int, int]:
        """Return the rendered bounds for ``text``."""


@dataclass(frozen=True, slots=True)
class HookTextLayout:
    """Wrapped hook text that fits a configured text box."""

    lines: tuple[str, ...]
    font_size: int
    line_height: int
    text_height: int
    truncated: bool


def fit_hook_text(
    text: str,
    load_font: Callable[[int], FontMetrics],
    config: HookConfig,
) -> HookTextLayout:
    """Wrap, shrink, and only then truncate text so it remains inside the hook box."""
    normalized_text = normalize_hook_text(text)
    if not normalized_text:
        raise ValueError("Hook text must not be blank.")

    sizes = range(config.font_size, config.minimum_font_size - 1, -1)
    if not config.automatic_font_shrinking:
        sizes = range(config.font_size, config.font_size - 1, -1)

    last_attempt: tuple[list[str], int, int, FontMetrics] | None = None
    maximum_width = _available_text_width(config)
    for font_size in sizes:
        font = load_font(font_size)
        lines = wrap_hook_text(normalized_text, font, maximum_width)
        line_height = _line_height(font, config.outline_width)
        text_height = _text_height(lines, line_height, config.line_spacing)
        last_attempt = (lines, line_height, text_height, font)
        if len(lines) <= config.maximum_lines and text_height <= _available_text_height(config):
            return HookTextLayout(
                lines=tuple(lines),
                font_size=font_size,
                line_height=line_height,
                text_height=text_height,
                truncated=False,
            )

    if last_attempt is None:
        raise ValueError("No valid hook font size is configured.")
    lines, line_height, _, font = last_attempt
    line_limit = min(
        config.maximum_lines,
        _maximum_lines_that_fit(_available_text_height(config), line_height, config.line_spacing),
    )
    if line_limit <= 0:
        raise ValueError("Hook text box is too small for the configured minimum font size.")
    truncated_lines = _truncate_lines(lines, line_limit, font, maximum_width)
    text_height = _text_height(truncated_lines, line_height, config.line_spacing)
    if text_height > _available_text_height(config):
        raise ValueError("Hook text does not fit inside the configured text box.")
    return HookTextLayout(
        lines=tuple(truncated_lines),
        font_size=config.minimum_font_size if config.automatic_font_shrinking else config.font_size,
        line_height=line_height,
        text_height=text_height,
        truncated=True,
    )


def normalize_hook_text(text: str) -> str:
    """Normalize spaces while retaining intentional paragraph breaks and Unicode characters."""
    paragraphs = [
        " ".join(paragraph.split())
        for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(paragraph for paragraph in paragraphs if paragraph)


def wrap_hook_text(text: str, font: FontMetrics, maximum_width: int) -> list[str]:
    """Wrap text at word boundaries, splitting a word only when it cannot fit alone."""
    if maximum_width <= 0:
        raise ValueError("maximum_width must be greater than zero.")

    lines: list[str] = []
    for paragraph in normalize_hook_text(text).split("\n"):
        current_line = ""
        for word in paragraph.split(" "):
            if not word:
                continue
            candidate = word if not current_line else f"{current_line} {word}"
            if font.getlength(candidate) <= maximum_width:
                current_line = candidate
                continue
            if current_line:
                lines.append(current_line)
                current_line = ""
            chunks = _split_long_word(word, font, maximum_width)
            lines.extend(chunks[:-1])
            current_line = chunks[-1]
        if current_line:
            lines.append(current_line)
    return lines


def _split_long_word(word: str, font: FontMetrics, maximum_width: int) -> list[str]:
    """Split an unbreakable word only as far as needed to prevent line overflow."""
    if font.getlength(word) <= maximum_width:
        return [word]

    chunks: list[str] = []
    current_chunk = ""
    for character in word:
        candidate = f"{current_chunk}{character}"
        if current_chunk and font.getlength(candidate) > maximum_width:
            chunks.append(current_chunk)
            current_chunk = character
        else:
            current_chunk = candidate
    if current_chunk:
        chunks.append(current_chunk)
    return chunks or [word]


def _line_height(font: FontMetrics, outline_width: int) -> int:
    """Measure an ascender/descender sample so every line has a stable height."""
    left, top, right, bottom = font.getbbox("Ag")
    del left, right
    return max(1, bottom - top) + (2 * outline_width)


def _text_height(lines: list[str], line_height: int, line_spacing: int) -> int:
    """Return the total pixel height occupied by multiline text."""
    if not lines:
        return 0
    return (len(lines) * line_height) + ((len(lines) - 1) * line_spacing)


def _available_text_height(config: HookConfig) -> int:
    """Return text height after padding inside the configured hook box."""
    shadow_space = config.shadow_offset if config.shadow_color is not None else 0
    return (
        config.text_box_height
        - (2 * config.text_padding)
        - (2 * config.outline_width)
        - shadow_space
    )


def _available_text_width(config: HookConfig) -> int:
    """Return line width after reserving optional outline and shadow pixels."""
    shadow_space = config.shadow_offset if config.shadow_color is not None else 0
    maximum_width = config.maximum_text_width - (2 * config.outline_width) - shadow_space
    if maximum_width <= 0:
        raise ValueError("Hook text width is too small for the configured outline or shadow.")
    return maximum_width


def _maximum_lines_that_fit(available_height: int, line_height: int, line_spacing: int) -> int:
    """Calculate how many complete lines fit without extending beyond the box."""
    if available_height < line_height:
        return 0
    return (available_height + line_spacing) // (line_height + line_spacing)


def _truncate_lines(
    lines: list[str],
    line_limit: int,
    font: FontMetrics,
    maximum_width: int,
) -> list[str]:
    """Retain safe whole lines and add an ASCII ellipsis to a shortened final line."""
    truncated_lines = list(lines[:line_limit])
    if len(lines) <= line_limit:
        return truncated_lines
    truncated_lines[-1] = _append_ellipsis(truncated_lines[-1], font, maximum_width)
    return truncated_lines


def _append_ellipsis(text: str, font: FontMetrics, maximum_width: int) -> str:
    """Append an ellipsis without allowing the final line to exceed its width."""
    suffix = "..."
    if font.getlength(suffix) > maximum_width:
        return ""
    shortened = text.rstrip()
    while shortened and font.getlength(f"{shortened}{suffix}") > maximum_width:
        shortened = shortened[:-1].rstrip()
    return f"{shortened}{suffix}" if shortened else suffix
