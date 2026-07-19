"""Pure tests for no-crop vertical layout calculations."""

from __future__ import annotations

from pathlib import Path
import unittest

from collector.models import FormatterConfig
from formatter.layout import calculate_fit_layout
from formatter.models import InputMediaProperties


def make_config() -> FormatterConfig:
    """Build the checked-in fit layout in a path-independent form."""
    return FormatterConfig(output_directory=Path("clips/ready"))


class FitLayoutTests(unittest.TestCase):
    """Verify every source aspect ratio remains fully visible and centered."""

    def test_landscape_video_fits_inside_the_vertical_video_area(self) -> None:
        """A 16:9 source uses full allowed width without cropping."""
        layout = calculate_fit_layout(InputMediaProperties(1920, 1080, True), make_config())

        self.assertEqual((layout.video_width, layout.video_height), (960, 540))
        self.assertEqual((layout.x, layout.y), (60, 790))
        self.assertEqual((layout.canvas_width, layout.canvas_height), (1080, 1920))

    def test_square_video_is_centered_inside_the_remaining_area(self) -> None:
        """A square source stays square and leaves the configured text area blank."""
        layout = calculate_fit_layout(InputMediaProperties(1080, 1080, True), make_config())

        self.assertEqual((layout.video_width, layout.video_height), (960, 960))
        self.assertEqual((layout.x, layout.y), (60, 580))

    def test_four_by_three_video_fits_without_crop(self) -> None:
        """Older 4:3 footage remains intact and centered on the vertical canvas."""
        layout = calculate_fit_layout(InputMediaProperties(640, 480, True), make_config())

        self.assertEqual((layout.video_width, layout.video_height), (960, 720))
        self.assertEqual((layout.x, layout.y), (60, 700))

    def test_vertical_video_uses_the_configured_maximum_height(self) -> None:
        """A 9:16 source remains fully visible rather than being cropped to the canvas."""
        layout = calculate_fit_layout(InputMediaProperties(1080, 1920, True), make_config())

        self.assertEqual((layout.video_width, layout.video_height), (786, 1400))
        self.assertEqual((layout.x, layout.y), (146, 360))

    def test_fit_mode_never_exceeds_available_bounds_or_stretches_the_source(self) -> None:
        """Odd dimensions are rounded safely while retaining the source aspect ratio."""
        source = InputMediaProperties(853, 480, False)
        layout = calculate_fit_layout(source, make_config())

        self.assertLessEqual(layout.video_width, layout.video_area_width)
        self.assertLessEqual(layout.video_height, layout.video_area_height)
        self.assertEqual(layout.video_width % 2, 0)
        self.assertEqual(layout.video_height % 2, 0)
        self.assertAlmostEqual(
            layout.video_width / layout.video_height,
            source.width / source.height,
            places=2,
        )

    def test_positions_center_video_without_entering_the_top_text_zone(self) -> None:
        """Centering preserves safe side margins and the blank hook-text area."""
        config = make_config()
        layout = calculate_fit_layout(InputMediaProperties(640, 480, True), config)

        self.assertGreaterEqual(layout.x, config.horizontal_margin)
        self.assertGreaterEqual(layout.y, config.top_text_area_height)
        self.assertEqual(layout.x, (config.output_width - layout.video_width) // 2)
