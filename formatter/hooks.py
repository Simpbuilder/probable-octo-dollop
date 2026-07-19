"""Hook-text selection, safe font fallback, and transparent PNG overlay rendering."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
from typing import Protocol

from collector.models import ClipMetadata, HookConfig, HookSource, HookStatus

from .text_layout import FontMetrics, fit_hook_text, normalize_hook_text

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only when dependencies are absent.
    Image = ImageColor = ImageDraw = ImageFont = None  # type: ignore[assignment]


class HookRenderError(RuntimeError):
    """Raised when selected hook text cannot be rendered into an overlay image."""


class HookRendererDependencyError(HookRenderError):
    """Raised when Pillow is unavailable for optional hook image generation."""


@dataclass(frozen=True, slots=True)
class HookSelection:
    """The text and provenance selected for one formatter pass."""

    text: str
    source: HookSource


@dataclass(frozen=True, slots=True)
class HookFontResolution:
    """A configured or system font choice, including a clear fallback diagnostic."""

    font_path: Path | None
    used_fallback: bool
    message: str | None = None


@dataclass(frozen=True, slots=True)
class HookRenderResult:
    """The temporary overlay and metadata values resulting from hook preparation."""

    overlay_file: Path | None
    text: str | None
    source: HookSource | None
    status: HookStatus
    error: str | None = None
    font_path: Path | None = None
    used_font_fallback: bool = False
    font_fallback_message: str | None = None


class HookRendererProtocol(Protocol):
    """The hook overlay surface required by the pending-clip formatter."""

    def render(
        self,
        selection: HookSelection,
        config: HookConfig,
        *,
        canvas_width: int,
        canvas_height: int,
        overlay_file: Path,
    ) -> HookRenderResult:
        """Render one transparent hook overlay for FFmpeg composition."""


class HookFontResolver:
    """Find configured fonts first, then safe platform-provided sans-serif candidates."""

    def __init__(self, system_font_paths: tuple[Path, ...] | None = None) -> None:
        """Allow deterministic system font candidates in tests while retaining defaults."""
        self._system_font_paths = (
            system_font_paths if system_font_paths is not None else _default_system_font_paths()
        )

    def candidates(self, configured_font_path: Path | None) -> tuple[Path, ...]:
        """Return de-duplicated configured and system font paths in preference order."""
        candidates: list[Path] = []
        if configured_font_path is not None:
            candidates.append(Path(configured_font_path))
        candidates.extend(self._system_font_paths)
        unique_candidates: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            normalized = Path(candidate)
            if normalized not in seen:
                unique_candidates.append(normalized)
                seen.add(normalized)
        return tuple(unique_candidates)


class PillowHookRenderer:
    """Render fit-to-box hook text into a temporary RGBA PNG using Pillow."""

    def __init__(self, font_resolver: HookFontResolver | None = None) -> None:
        """Use an injectable resolver so font fallback is deterministic in tests."""
        self._font_resolver = font_resolver or HookFontResolver()

    def render(
        self,
        selection: HookSelection,
        config: HookConfig,
        *,
        canvas_width: int,
        canvas_height: int,
        overlay_file: Path,
    ) -> HookRenderResult:
        """Create a canvas-sized transparent PNG with wrapped text in the hook region."""
        pillow = _require_pillow()
        font_loader, resolution = self._font_loader(config, pillow)
        try:
            layout = fit_hook_text(selection.text, font_loader, config)
            font = font_loader(layout.font_size)
            _validate_color(config.font_color, pillow)
            if config.outline_color is not None:
                _validate_color(config.outline_color, pillow)
            if config.shadow_color is not None:
                _validate_color(config.shadow_color, pillow)

            image = pillow["Image"].new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 0))
            draw = pillow["ImageDraw"].Draw(image)
            _draw_layout(
                draw,
                lines=layout.lines,
                font=font,
                line_height=layout.line_height,
                text_height=layout.text_height,
                canvas_width=canvas_width,
                config=config,
            )
            overlay_file = Path(overlay_file)
            overlay_file.parent.mkdir(parents=True, exist_ok=True)
            image.save(overlay_file, format="PNG")
        except (OSError, ValueError) as error:
            raise HookRenderError(f"Could not render hook text: {error}") from error

        return HookRenderResult(
            overlay_file=overlay_file.resolve(),
            text=normalize_hook_text(selection.text),
            source=selection.source,
            status="rendered",
            font_path=resolution.font_path,
            used_font_fallback=resolution.used_fallback,
            font_fallback_message=resolution.message,
        )

    def _font_loader(self, config: HookConfig, pillow: dict[str, object]):
        """Choose a loadable font file or Pillow's built-in fallback without failing clips."""
        image_font = pillow["ImageFont"]
        configured_path = Path(config.font_path) if config.font_path is not None else None
        configured_missing = configured_path is not None and not configured_path.is_file()
        configured_unusable = False

        for candidate in self._font_resolver.candidates(configured_path):
            if not candidate.is_file():
                continue
            try:
                image_font.truetype(str(candidate), config.font_size)
            except OSError:
                if candidate == configured_path:
                    configured_unusable = True
                continue

            used_fallback = configured_path is not None and candidate != configured_path
            message = None
            if used_fallback:
                message = f"Configured hook font unavailable; using system font: {candidate.name}"
            return (
                lambda size, path=candidate: image_font.truetype(str(path), size),
                HookFontResolution(candidate.resolve(), used_fallback, message),
            )

        message = None
        if configured_missing or configured_unusable:
            message = "Configured hook font unavailable; using Pillow's built-in fallback font."
        elif configured_path is None:
            message = "No system hook font found; using Pillow's built-in fallback font."
        return (
            lambda size: _load_default_font(image_font, size),
            HookFontResolution(None, True, message),
        )


def resolve_hook_selection(
    clip: ClipMetadata,
    config: HookConfig,
    manual_hook: str | None = None,
) -> HookSelection | None:
    """Prefer an explicit hook, then a reviewed candidate, legacy text, or a source title."""
    if manual_hook is not None:
        normalized_manual_hook = normalize_hook_text(manual_hook)
        if normalized_manual_hook:
            return HookSelection(normalized_manual_hook, "manual")
        return None
    if not config.enabled:
        return None
    if clip.selected_hook is not None:
        normalized_selected_hook = normalize_hook_text(clip.selected_hook)
        if normalized_selected_hook:
            return HookSelection(normalized_selected_hook, "generated")
    if clip.hook_text is not None:
        normalized_hook_text = normalize_hook_text(clip.hook_text)
        if normalized_hook_text:
            return HookSelection(normalized_hook_text, clip.hook_source or "manual")
    if config.fallback_to_source_title:
        normalized_title = normalize_hook_text(clip.title)
        if normalized_title:
            return HookSelection(normalized_title, "source_title")
    return None


def skipped_hook_result(clip: ClipMetadata) -> HookRenderResult:
    """Record a no-hook pass while preserving a manually stored hook for a later retry."""
    return HookRenderResult(
        overlay_file=None,
        text=clip.hook_text,
        source=clip.hook_source,
        status="skipped",
    )


def failed_hook_result(
    selection: HookSelection | None,
    error: str,
) -> HookRenderResult:
    """Build retryable metadata for a hook-specific rendering failure."""
    return HookRenderResult(
        overlay_file=None,
        text=selection.text if selection is not None else None,
        source=selection.source if selection is not None else None,
        status="failed",
        error=error,
    )


def _default_system_font_paths() -> tuple[Path, ...]:
    """Return non-committed bold sans-serif candidates supplied by common operating systems."""
    system_name = platform.system()
    if system_name == "Windows":
        windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        return (
            windows_directory / "segoeuib.ttf",
            windows_directory / "seguisb.ttf",
            windows_directory / "arialbd.ttf",
        )
    if system_name == "Darwin":
        return (
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path("/Library/Fonts/Arial Bold.ttf"),
        )
    return (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    )


def _require_pillow() -> dict[str, object]:
    """Return Pillow modules or raise a focused dependency error at render time."""
    if Image is None or ImageColor is None or ImageDraw is None or ImageFont is None:
        raise HookRendererDependencyError(
            "Pillow is required for hook text rendering. Install dependencies from requirements.txt."
        )
    return {
        "Image": Image,
        "ImageColor": ImageColor,
        "ImageDraw": ImageDraw,
        "ImageFont": ImageFont,
    }


def _load_default_font(image_font: object, size: int) -> FontMetrics:
    """Load Pillow's built-in fallback font across supported Pillow versions."""
    try:
        return image_font.load_default(size=size)
    except TypeError:
        return image_font.load_default()


def _validate_color(color: str, pillow: dict[str, object]) -> None:
    """Validate Pillow color syntax before creating any output file."""
    pillow["ImageColor"].getrgb(color)


def _draw_layout(
    draw: object,
    *,
    lines: tuple[str, ...],
    font: FontMetrics,
    line_height: int,
    text_height: int,
    canvas_width: int,
    config: HookConfig,
) -> None:
    """Draw each wrapped line in the padded hook box without font-specific anchor support."""
    shadow_space = config.shadow_offset if config.shadow_color is not None else 0
    available_height = (
        config.text_box_height
        - (2 * config.text_padding)
        - (2 * config.outline_width)
        - shadow_space
    )
    top = config.vertical_position + config.text_padding + config.outline_width
    y = top + max(0, (available_height - text_height) // 2)
    box_left = ((canvas_width - config.maximum_text_width) // 2) + config.outline_width
    box_right = (
        ((canvas_width + config.maximum_text_width) // 2)
        - config.outline_width
        - shadow_space
    )
    draw_options = {
        "font": font,
        "fill": config.font_color,
        "stroke_width": config.outline_width,
        "stroke_fill": config.outline_color or config.font_color,
    }
    font_top = font.getbbox("Ag")[1]
    for index, line in enumerate(lines):
        line_y = (
            y
            - font_top
            + config.outline_width
            + (index * (line_height + config.line_spacing))
        )
        line_width = font.getlength(line)
        if config.horizontal_alignment == "left":
            line_x = box_left
        elif config.horizontal_alignment == "right":
            line_x = box_right - line_width
        else:
            line_x = ((box_left + box_right) - line_width) / 2
        if config.shadow_color is not None and config.shadow_offset:
            draw.text(
                (line_x + config.shadow_offset, line_y + config.shadow_offset),
                line,
                fill=config.shadow_color,
                **{key: value for key, value in draw_options.items() if key != "fill"},
            )
        draw.text((line_x, line_y), line, **draw_options)
