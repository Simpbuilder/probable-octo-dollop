"""Offline tests for hook selection, wrapping, font fallback, and transparent overlays."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from collector.models import ClipMetadata, HookConfig
from formatter.hooks import (
    HookFontResolver,
    HookSelection,
    PillowHookRenderer,
    resolve_hook_selection,
)
from formatter.text_layout import fit_hook_text


class ScaledMonospaceFont:
    """A deterministic font metric model for pure wrapping tests."""

    def __init__(self, size: int) -> None:
        self.size = size

    def getlength(self, text: str) -> float:
        """Treat every Unicode code point as an equal-width glyph for test clarity."""
        return len(text) * self.size * 0.5

    def getbbox(self, text: str) -> tuple[int, int, int, int]:
        """Return a stable ascender/descender box scaled by font size."""
        return (0, 0, max(1, int(self.getlength(text))), self.size)


def font_loader(size: int) -> ScaledMonospaceFont:
    """Build a deterministic font for the pure text-layout tests."""
    return ScaledMonospaceFont(size)


def make_hook_config(**overrides: object) -> HookConfig:
    """Build a compact hook box that makes wrapping behavior easy to assert."""
    values: dict[str, object] = {
        "enabled": True,
        "font_size": 40,
        "minimum_font_size": 20,
        "maximum_text_width": 400,
        "maximum_lines": 3,
        "line_spacing": 5,
        "vertical_position": 0,
        "text_box_height": 180,
        "text_padding": 10,
    }
    values.update(overrides)
    return HookConfig(**values)  # type: ignore[arg-type]


def make_clip(*, title: str = "Source title", hook_text: str | None = None) -> ClipMetadata:
    """Create a minimum valid clip record for source-selection tests."""
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    return ClipMetadata(
        unique_id="hook-selection",
        source="manual",
        subreddit=None,
        source_post_id="hook-selection",
        source_url="https://example.invalid/hook-selection",
        title=title,
        author="manual_intake",
        score=0,
        comment_count=0,
        created_at=now,
        media_url=None,
        local_file_path=None,
        added_at=now,
        hook_text=hook_text,
    )


class HookTextLayoutTests(unittest.TestCase):
    """Verify hook text stays readable and bounded without an FFmpeg dependency."""

    def test_short_hook_stays_on_one_line(self) -> None:
        """A short one-line hook remains at the configured font size."""
        config = make_hook_config()

        layout = fit_hook_text("That was unexpected", font_loader, config)

        self.assertEqual(layout.lines, ("That was unexpected",))
        self.assertEqual(layout.font_size, config.font_size)
        self.assertFalse(layout.truncated)

    def test_hook_wraps_cleanly_to_two_lines(self) -> None:
        """Whole words are retained where possible when a hook needs a second line."""
        config = make_hook_config(maximum_text_width=220)

        layout = fit_hook_text("This small surprise", font_loader, config)

        self.assertEqual(layout.lines, ("This small", "surprise"))
        self.assertFalse(layout.truncated)

    def test_long_hook_shrinks_before_it_truncates(self) -> None:
        """Automatic sizing makes a two-line hook fit before truncation is considered."""
        config = make_hook_config(
            font_size=60,
            minimum_font_size=20,
            maximum_text_width=300,
            maximum_lines=2,
            text_box_height=90,
            text_padding=10,
        )

        layout = fit_hook_text("This takes two short lines", font_loader, config)

        self.assertLess(layout.font_size, config.font_size)
        self.assertLessEqual(len(layout.lines), 2)
        self.assertFalse(layout.truncated)

    def test_too_long_hook_truncates_safely_at_the_minimum_size(self) -> None:
        """A final ASCII ellipsis keeps an unavoidably long hook inside one line."""
        config = make_hook_config(
            font_size=32,
            minimum_font_size=32,
            automatic_font_shrinking=False,
            maximum_text_width=200,
            maximum_lines=1,
        )

        layout = fit_hook_text("This title keeps going beyond the available hook space", font_loader, config)

        self.assertTrue(layout.truncated)
        self.assertEqual(len(layout.lines), 1)
        self.assertTrue(layout.lines[0].endswith("..."))
        self.assertLessEqual(font_loader(layout.font_size).getlength(layout.lines[0]), 200)

    def test_unicode_apostrophes_and_quotes_are_preserved(self) -> None:
        """Unicode text remains intact through selection and layout normalization."""
        config = make_hook_config(maximum_text_width=900)
        text = 'She said "caf\u00e9" wasn\'t over yet \u4f60\u597d'

        layout = fit_hook_text(text, font_loader, config)

        self.assertEqual(" ".join(layout.lines), text)


class HookSelectionAndRenderingTests(unittest.TestCase):
    """Verify manual and fallback sources produce a usable transparent overlay."""

    def test_manual_text_wins_then_selected_hook_and_title_fallback_are_used(self) -> None:
        """An explicit hook wins, then a reviewed candidate, then the source-title fallback."""
        clip = make_clip(title="A useful title", hook_text="Stored manual hook")
        config = make_hook_config()

        manual = resolve_hook_selection(clip, config, manual_hook="CLI hook")
        stored = resolve_hook_selection(clip, config)
        selected = resolve_hook_selection(
            replace(clip, selected_hook="Reviewed generated hook"), config
        )
        fallback = resolve_hook_selection(make_clip(title="A useful title"), config)

        self.assertEqual(manual, HookSelection("CLI hook", "manual"))
        self.assertEqual(stored, HookSelection("Stored manual hook", "manual"))
        self.assertEqual(selected, HookSelection("Reviewed generated hook", "generated"))
        self.assertEqual(fallback, HookSelection("A useful title", "source_title"))

    def test_disabled_hook_and_missing_text_return_no_selection(self) -> None:
        """No hook remains a supported formatter path when hooks are disabled or unavailable."""
        self.assertIsNone(resolve_hook_selection(make_clip(), make_hook_config(enabled=False)))
        self.assertIsNone(
            resolve_hook_selection(
                make_clip(),
                make_hook_config(fallback_to_source_title=False),
            )
        )

    def test_missing_configured_font_uses_a_builtin_fallback_overlay(self) -> None:
        """A missing optional font file does not prevent a hook overlay from being created."""
        renderer = PillowHookRenderer(HookFontResolver(system_font_paths=()))
        config = make_hook_config(font_path=Path("missing-font.ttf"))

        with TemporaryDirectory() as temporary_directory:
            overlay_file = Path(temporary_directory) / "hook.png"
            result = renderer.render(
                HookSelection("He looked away for one second...", "manual"),
                config,
                canvas_width=1080,
                canvas_height=1920,
                overlay_file=overlay_file,
            )
            with Image.open(overlay_file) as image:
                alpha_bounds = image.getchannel("A").getbbox()
                self.assertEqual(image.size, (1080, 1920))

        self.assertEqual(result.status, "rendered")
        self.assertTrue(result.used_font_fallback)
        self.assertIsNone(result.font_path)
        self.assertIn("fallback", result.font_fallback_message)
        self.assertIsNotNone(alpha_bounds)
        self.assertGreaterEqual(alpha_bounds[0], (1080 - config.maximum_text_width) // 2)
        self.assertLessEqual(alpha_bounds[2], (1080 + config.maximum_text_width) // 2)
        self.assertGreaterEqual(alpha_bounds[1], config.vertical_position)
        self.assertLessEqual(alpha_bounds[3], config.vertical_position + config.text_box_height)

    def test_renderer_accepts_unicode_apostrophes_and_quotes(self) -> None:
        """A system-font overlay keeps Unicode hook text out of shell and FFmpeg filter syntax."""
        renderer = PillowHookRenderer()
        config = make_hook_config(maximum_text_width=900)
        text = '"Caf\u00e9" wasn\'t ready for \u4f60\u597d'

        with TemporaryDirectory() as temporary_directory:
            result = renderer.render(
                HookSelection(text, "manual"),
                config,
                canvas_width=1080,
                canvas_height=1920,
                overlay_file=Path(temporary_directory) / "unicode-hook.png",
            )

        self.assertEqual(result.status, "rendered")
        self.assertEqual(result.text, text)
