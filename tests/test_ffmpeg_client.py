"""Offline tests for FFmpeg command construction and ffprobe result handling."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collector.models import FormatterConfig
from formatter.ffmpeg_client import (
    FfmpegClient,
    FfmpegDependencyError,
    FfmpegProcessError,
)
from formatter.models import FormatRequest, InputMediaProperties, VideoLayout


def make_request(root: Path) -> FormatRequest:
    """Build one deterministic format request without requiring a valid source file."""
    config = FormatterConfig(output_directory=root / "ready")
    return FormatRequest(
        input_file=root / "pending" / "source clip.mp4",
        output_file=root / "ready" / "formatted clip.mp4",
        input_properties=InputMediaProperties(width=1920, height=1080, has_audio=False),
        layout=VideoLayout(1080, 1920, 960, 540, 60, 790, 960, 1400),
        config=config,
    )


class FfmpegClientTests(unittest.TestCase):
    """Verify subprocess calls are safe argument lists without invoking FFmpeg."""

    def test_builds_no_crop_h264_aac_command_as_a_list(self) -> None:
        """The command is Windows-safe and includes compatible output settings."""
        with TemporaryDirectory() as temporary_directory:
            request = make_request(Path(temporary_directory))
            client = FfmpegClient(ffmpeg_executable="ffmpeg.exe", ffprobe_executable="ffprobe.exe")

            command = client.build_format_command(request)

        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(argument, str) for argument in command))
        self.assertEqual(command[0], "ffmpeg.exe")
        self.assertIn("-nostdin", command)
        self.assertIn("-filter_complex", command)
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertNotIn("crop", filter_graph)
        self.assertIn("scale=960:540", filter_graph)
        self.assertIn("overlay=x=60:y=790", filter_graph)
        self.assertIn("0:a?", command)
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertIn("yuv420p", command)
        self.assertIn("+faststart", command)
        self.assertEqual(command[-1], str(request.output_file))

    def test_inspects_video_without_audio_from_ffprobe_json(self) -> None:
        """Optional source audio is represented without treating it as an error."""
        with TemporaryDirectory() as temporary_directory:
            input_file = Path(temporary_directory) / "source.mp4"
            input_file.write_bytes(b"fixture")
            client = FfmpegClient(ffmpeg_executable="ffmpeg", ffprobe_executable="ffprobe")
            completed = __import__("subprocess").CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "width": 853,
                                "height": 480,
                                "avg_frame_rate": "30000/1001",
                            }
                        ]
                    }
                ),
                stderr="",
            )

            with patch("formatter.ffmpeg_client.subprocess.run", return_value=completed) as run:
                properties = client.inspect(input_file)

        self.assertEqual((properties.width, properties.height), (853, 480))
        self.assertFalse(properties.has_audio)
        self.assertEqual(properties.frame_rate, "30000/1001")
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], str(input_file))

    def test_builds_windows_safe_hook_overlay_command_without_inlining_user_text(self) -> None:
        """A transparent hook image is a second input, not an escaped drawtext expression."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = replace(
                make_request(root),
                hook_overlay_file=root / "hook overlay.png",
            )
            client = FfmpegClient(ffmpeg_executable="ffmpeg.exe", ffprobe_executable="ffprobe.exe")

            command = client.build_format_command(request)

        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(argument, str) for argument in command))
        self.assertIn("-loop", command)
        self.assertIn("-framerate", command)
        self.assertIn(str(request.hook_overlay_file), command)
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[1:v]format=rgba,setpts=PTS-STARTPTS[hook]", filter_graph)
        self.assertIn("[base][hook]overlay=x=0:y=0", filter_graph)
        self.assertNotIn("drawtext", filter_graph)

    def test_missing_ffmpeg_or_ffprobe_is_reported_before_processing(self) -> None:
        """The adapter fails with a focused setup message when tools are unavailable."""
        with patch("formatter.ffmpeg_client.shutil.which", return_value=None):
            client = FfmpegClient()

        with self.assertRaisesRegex(FfmpegDependencyError, "ffmpeg"):
            client.ensure_available()

    def test_ffmpeg_failure_is_raised_without_shell_execution(self) -> None:
        """A nonzero FFmpeg exit becomes a formatter-level recoverable error."""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = make_request(root)
            request.input_file.parent.mkdir(parents=True)
            request.input_file.write_bytes(b"fixture")
            client = FfmpegClient(ffmpeg_executable="ffmpeg", ffprobe_executable="ffprobe")
            completed = __import__("subprocess").CompletedProcess(
                args=[], returncode=1, stdout="", stderr="invalid media"
            )

            with patch("formatter.ffmpeg_client.subprocess.run", return_value=completed) as run:
                with self.assertRaisesRegex(FfmpegProcessError, "invalid media"):
                    client.format(request)

        self.assertIsInstance(run.call_args.args[0], list)
