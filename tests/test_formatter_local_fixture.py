"""Optional local FFmpeg fixture test; no media fixture is committed to the repository."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from collector.models import FormatterConfig
from formatter.ffmpeg_client import FfmpegClient
from formatter.layout import calculate_fit_layout
from formatter.models import FormatRequest


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "Requires local ffmpeg and ffprobe executables.",
)
class LocalFfmpegFixtureTests(unittest.TestCase):
    """Generate a tiny temporary source and verify a real local formatting pass."""

    def test_formats_a_tiny_silent_landscape_fixture(self) -> None:
        """The output is 1080x1920 and has no audio when the source has no audio."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = root / "source.mp4"
            output_file = root / "ready" / "formatted.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=64x36:r=5",
                    "-t",
                    "0.2",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source_file),
                ],
                check=True,
            )
            client = FfmpegClient()
            input_properties = client.inspect(source_file)
            config = FormatterConfig(output_directory=output_file.parent)
            result = client.format(
                FormatRequest(
                    input_file=source_file,
                    output_file=output_file,
                    input_properties=input_properties,
                    layout=calculate_fit_layout(input_properties, config),
                    config=config,
                )
            )
            output_properties = client.inspect(result.output_file)

            self.assertEqual((output_properties.width, output_properties.height), (1080, 1920))
            self.assertFalse(output_properties.has_audio)
