"""Mocked tests for reusable-token YouTube Shorts uploads without network access."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collector.models import ClipMetadata, YoutubeConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from publisher.youtube.client import YoutubeClient, YoutubeClientError
from publisher.youtube.history import load_youtube_history
from publisher.youtube.models import (
    YoutubeAuthenticationStatus,
    YoutubeChannel,
    YoutubeUploadResult,
)
from publisher.youtube.uploader import YoutubeUploader
from publisher.youtube.oauth import login_to_youtube
from run_pipeline import main as run_pipeline_main, run_youtube_login
from ui_helpers import load_youtube_overview


def make_config(root: Path, **overrides: object) -> YoutubeConfig:
    """Build a safe isolated uploader configuration without referring to real OAuth files."""
    defaults: dict[str, object] = {
        "enabled": True,
        "source_directory": root / "clips" / "ready" / "hooked",
        "privacy_status": "public",
        "category_id": "24",
        "default_title_template": "{title}",
        "default_description": "Configured description",
        "tags": ("funny",),
        "maximum_uploads_per_run": 2,
        "delay_between_uploads_seconds": 30,
        "move_after_upload": False,
        "posted_directory": root / "clips" / "posted",
        "duplicate_check_enabled": True,
        "made_for_kids": False,
        "oauth_client_credentials_file": root / "oauth-client.json",
        "token_file": root / "token.json",
        "external_history_file": root / "external-history.json",
    }
    defaults.update(overrides)
    return YoutubeConfig(**defaults)  # type: ignore[arg-type]


def make_clip(video_file: Path, **overrides: object) -> ClipMetadata:
    """Create one formatted hooked clip with predictable source metadata for title checks."""
    defaults: dict[str, object] = {
        "unique_id": "youtube-clip",
        "source": "manual",
        "subreddit": None,
        "source_post_id": "youtube-clip",
        "source_url": "https://example.com/source",
        "title": "Original source title",
        "author": "author",
        "score": 1,
        "comment_count": 0,
        "created_at": datetime.now(timezone.utc),
        "media_url": None,
        "local_file_path": None,
        "download_status": "downloaded",
        "processing_status": "ready",
        "formatted_file_path": video_file,
        "formatted_width": 1080,
        "formatted_height": 1920,
    }
    defaults.update(overrides)
    if "source_post_id" not in overrides:
        defaults["source_post_id"] = defaults["unique_id"]
    return ClipMetadata(**defaults)  # type: ignore[arg-type]


class FakeYoutubeClient:
    """In-memory YouTube transport that records requests instead of performing API calls."""

    def __init__(self, *, fail_filenames: set[str] | None = None) -> None:
        self.fail_filenames = fail_filenames or set()
        self.requests: list[dict[str, object]] = []
        self.remote_ids = frozenset()

    def authentication_status(self, *, include_channel: bool = True) -> YoutubeAuthenticationStatus:
        return YoutubeAuthenticationStatus(
            credentials_available=True,
            token_available=True,
            token_reusable=True,
            channel=YoutubeChannel("channel-1", "Existing channel") if include_channel else None,
        )

    def upload_short(self, video_file: Path, **kwargs: object) -> YoutubeUploadResult:
        self.requests.append({"video_file": video_file, **kwargs})
        if video_file.name in self.fail_filenames:
            raise YoutubeClientError("simulated upload failure")
        video_id = f"video-{len(self.requests)}"
        return YoutubeUploadResult(video_id, f"https://www.youtube.com/watch?v={video_id}")

    def list_uploaded_video_ids(self) -> frozenset[str]:
        return self.remote_ids


class YoutubeClientTests(unittest.TestCase):
    """Verify credential reuse and exact YouTube request fields without Google network access."""

    def test_missing_credentials_report_clean_status_without_starting_oauth(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config = make_config(Path(temporary_directory))
            status = YoutubeClient(config).authentication_status(include_channel=False)

        self.assertFalse(status.credentials_available)
        self.assertFalse(status.token_available)
        self.assertFalse(status.token_reusable)
        self.assertIn("Missing", status.error or "")

    def test_existing_token_is_reused_in_memory_without_writing_the_source_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = make_config(root)
            config.oauth_client_credentials_file.write_text("{}", encoding="utf-8")
            config.token_file.write_text("existing-token", encoding="utf-8")

            class FakeCredentials:
                valid = True
                expired = False
                refresh_token = "refresh"

                @classmethod
                def from_authorized_user_file(cls, path: str, scopes: list[str]):
                    self.assertEqual(Path(path), config.token_file)
                    self.assertEqual(
                        scopes,
                        [
                            "https://www.googleapis.com/auth/youtube.upload",
                            "https://www.googleapis.com/auth/youtube.readonly",
                        ],
                    )
                    return cls()

            with patch(
                "publisher.youtube.client._google_auth_dependencies",
                return_value=(FakeCredentials, object),
            ):
                status = YoutubeClient(config).authentication_status(include_channel=False)

            self.assertTrue(status.token_reusable)
            self.assertEqual(config.token_file.read_text(encoding="utf-8"), "existing-token")

    def test_upload_request_is_public_and_explicitly_not_for_kids(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = make_config(root)
            client = YoutubeClient(config)
            captured: dict[str, object] = {}

            class FakeRequest:
                def next_chunk(self):
                    return None, {"id": "video-1"}

            class FakeVideos:
                def insert(self, **kwargs: object):
                    captured.update(kwargs)
                    return FakeRequest()

            class FakeService:
                def videos(self):
                    return FakeVideos()

            class FakeMediaFileUpload:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

            client._service = FakeService()
            with patch(
                "publisher.youtube.client._google_dependencies",
                return_value=(object, FakeMediaFileUpload),
            ):
                result = client.upload_short(
                    root / "short.mp4",
                    title="Short title",
                    description="Description",
                    tags=("funny",),
                    category_id="24",
                    privacy_status="public",
                    made_for_kids=False,
                )

        self.assertEqual(result.video_id, "video-1")
        body = captured["body"]
        self.assertEqual(body["status"]["privacyStatus"], "public")  # type: ignore[index]
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])  # type: ignore[index]


class YoutubeOAuthLoginTests(unittest.TestCase):
    """Verify browser login and token persistence without opening a browser or using the network."""

    def test_browser_login_saves_root_token_and_returns_channel(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            client_secret_file = root / "client_secret.json"
            token_file = root / "token.json"
            client_secret_file.write_text("{}", encoding="utf-8")
            captured: dict[str, object] = {}

            class FakeCredentials:
                def to_json(self) -> str:
                    return '{"token": "test-token"}'

            class FakeFlow:
                @classmethod
                def from_client_secrets_file(cls, path: str, scopes: list[str]):
                    captured["client_secret_file"] = Path(path)
                    captured["scopes"] = scopes
                    return cls()

                def run_local_server(self, **kwargs: object) -> FakeCredentials:
                    captured["login_options"] = kwargs
                    return FakeCredentials()

            class FakeChannelRequest:
                def execute(self) -> dict[str, object]:
                    return {
                        "items": [
                            {"id": "channel-123", "snippet": {"title": "Creator channel"}}
                        ]
                    }

            class FakeChannels:
                def list(self, **kwargs: object) -> FakeChannelRequest:
                    captured["channel_request"] = kwargs
                    return FakeChannelRequest()

            class FakeService:
                def channels(self) -> FakeChannels:
                    return FakeChannels()

            def fake_build(api_name: str, version: str, **kwargs: object) -> FakeService:
                captured["service"] = (api_name, version, kwargs)
                return FakeService()

            with patch(
                "publisher.youtube.oauth._oauth_dependencies",
                return_value=(FakeFlow, fake_build),
            ):
                channel = login_to_youtube(client_secret_file, token_file)

            self.assertEqual(channel, YoutubeChannel("channel-123", "Creator channel"))
            self.assertEqual(captured["client_secret_file"], client_secret_file)
            self.assertEqual(
                captured["scopes"],
                [
                    "https://www.googleapis.com/auth/youtube.upload",
                    "https://www.googleapis.com/auth/youtube.readonly",
                ],
            )
            self.assertEqual(
                captured["login_options"],
                {
                    "port": 0,
                    "open_browser": True,
                    "access_type": "offline",
                    "prompt": "consent",
                },
            )
            self.assertEqual(token_file.read_text(encoding="utf-8"), '{"token": "test-token"}')
            self.assertFalse((root / "token.json.tmp").exists())

    def test_missing_client_secret_fails_before_oauth_or_token_creation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch("publisher.youtube.oauth._oauth_dependencies") as dependencies:
                with self.assertRaisesRegex(YoutubeClientError, "client file not found"):
                    login_to_youtube(root / "client_secret.json", root / "token.json")

            dependencies.assert_not_called()
            self.assertFalse((root / "token.json").exists())

    def test_login_runner_prints_channel_and_uses_only_root_oauth_files(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = type("Config", (), {"youtube_config": make_config(root)})()
            output: list[str] = []
            with patch(
                "run_pipeline.login_to_youtube",
                return_value=YoutubeChannel("channel-1", "Test channel"),
            ) as login:
                with patch("builtins.print", side_effect=output.append):
                    exit_code = run_youtube_login(config, root)

        self.assertEqual(exit_code, 0)
        login.assert_called_once_with(root / "client_secret.json", root / "token.json")
        self.assertIn("Channel name: Test channel", output)
        self.assertIn("Channel ID: channel-1", output)


class YoutubeUploaderTests(unittest.TestCase):
    """Verify history, metadata, title selection, queue controls, and delays with fake uploads."""

    def make_uploader(
        self,
        root: Path,
        config: YoutubeConfig,
        client: FakeYoutubeClient,
        *,
        sleep_calls: list[float] | None = None,
    ) -> YoutubeUploader:
        return YoutubeUploader(
            metadata_file=root / "metadata" / "clips.json",
            history_file=root / "metadata" / "youtube_upload_history.json",
            config=config,
            client=client,
            sleep_func=(sleep_calls.append if sleep_calls is not None else lambda _seconds: None),
        )

    def test_success_records_history_metadata_and_selected_hook_title(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clips" / "ready" / "hooked"
            source.mkdir(parents=True)
            video = source / "clip.mp4"
            video.write_bytes(b"video")
            metadata_file = root / "metadata" / "clips.json"
            save_clip_metadata(metadata_file, make_clip(video, selected_hook="He said WHAT?"))
            client = FakeYoutubeClient()
            summary = self.make_uploader(root, make_config(root), client).run()
            saved = load_all_clip_metadata(metadata_file)[0]
            history = load_youtube_history(root / "metadata" / "youtube_upload_history.json")
            video_exists = video.exists()

        self.assertEqual(summary.uploaded, 1)
        self.assertTrue(video_exists)
        self.assertEqual(client.requests[0]["title"], "He said WHAT?")
        self.assertEqual(client.requests[0]["privacy_status"], "public")
        self.assertFalse(client.requests[0]["made_for_kids"])
        self.assertEqual(saved.youtube_video_id, "video-1")
        self.assertEqual(saved.youtube_video_url, "https://www.youtube.com/watch?v=video-1")
        self.assertEqual(saved.youtube_upload_status, "uploaded")
        self.assertEqual(history[0]["local_filename"], "clip.mp4")

    def test_title_fallback_prefers_hook_text_then_source_title_then_filename(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clips" / "ready" / "hooked"
            source.mkdir(parents=True)
            first = source / "first.mp4"
            second = source / "second.mp4"
            third = source / "third-file.mp4"
            for path in (first, second, third):
                path.write_bytes(path.name.encode())
            metadata_file = root / "metadata" / "clips.json"
            save_clip_metadata(metadata_file, make_clip(first, unique_id="one", hook_text="Instructions unclear"))
            save_clip_metadata(metadata_file, make_clip(second, unique_id="two", title="Source fallback"))
            client = FakeYoutubeClient()
            self.make_uploader(root, make_config(root), client).run(process_all=True)

        self.assertEqual([request["title"] for request in client.requests], [
            "Instructions unclear",
            "Source fallback",
            "third-file",
        ])

    def test_duplicate_history_is_skipped_without_delay(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clips" / "ready" / "hooked"
            source.mkdir(parents=True)
            duplicate = source / "duplicate.mp4"
            fresh = source / "fresh.mp4"
            duplicate.write_bytes(b"duplicate")
            fresh.write_bytes(b"fresh")
            history_file = root / "metadata" / "youtube_upload_history.json"
            history_file.parent.mkdir(parents=True)
            history_file.write_text(
                '{"uploads": [{"status": "uploaded", "local_filename": "duplicate.mp4", "youtube_video_id": "old"}]}',
                encoding="utf-8",
            )
            sleep_calls: list[float] = []
            client = FakeYoutubeClient()
            summary = self.make_uploader(root, make_config(root), client, sleep_calls=sleep_calls).run(process_all=True)

        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(summary.uploaded, 1)
        self.assertEqual(sleep_calls, [])

    def test_one_failed_upload_does_not_stop_later_files_and_failure_stays_retryable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clips" / "ready" / "hooked"
            source.mkdir(parents=True)
            bad = source / "bad.mp4"
            good = source / "good.mp4"
            bad.write_bytes(b"bad")
            good.write_bytes(b"good")
            metadata_file = root / "metadata" / "clips.json"
            save_clip_metadata(metadata_file, make_clip(bad, unique_id="bad"))
            save_clip_metadata(metadata_file, make_clip(good, unique_id="good"))
            client = FakeYoutubeClient(fail_filenames={"bad.mp4"})
            summary = self.make_uploader(root, make_config(root), client).run(process_all=True)
            clips = {clip.unique_id: clip for clip in load_all_clip_metadata(metadata_file)}

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.uploaded, 1)
        self.assertEqual(clips["bad"].youtube_upload_status, "failed")
        self.assertIsNotNone(clips["bad"].youtube_upload_error)
        self.assertEqual(clips["good"].youtube_upload_status, "uploaded")

    def test_configured_limit_and_all_control_batch_work_and_delay_only_between_successes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clips" / "ready" / "hooked"
            source.mkdir(parents=True)
            for number in range(3):
                (source / f"clip-{number}.mp4").write_bytes(str(number).encode())
            limited_client = FakeYoutubeClient()
            limited_calls: list[float] = []
            config = make_config(root, maximum_uploads_per_run=2, delay_between_uploads_seconds=7)
            limited = self.make_uploader(root, config, limited_client, sleep_calls=limited_calls).run()
            full_root = root / "full"
            full_source = full_root / "clips" / "ready" / "hooked"
            full_source.mkdir(parents=True)
            for number in range(3):
                (full_source / f"clip-{number}.mp4").write_bytes(str(number).encode())
            all_client = FakeYoutubeClient()
            all_calls: list[float] = []
            full = self.make_uploader(
                full_root,
                make_config(full_root, delay_between_uploads_seconds=7),
                all_client,
                sleep_calls=all_calls,
            ).run(process_all=True)

        self.assertEqual(limited.processing, 2)
        self.assertEqual(limited.remaining, 1)
        self.assertEqual(limited_calls, [7])
        self.assertEqual(full.processing, 3)
        self.assertEqual(full.uploaded, 3)
        self.assertEqual(all_calls, [7, 7])


class YoutubeUiIntegrationTests(unittest.TestCase):
    """Verify the UI overview delegates status to the reusable client without upload calls."""

    def test_ui_overview_shows_mocked_auth_and_local_pending_count(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = make_config(root)
            config.source_directory.mkdir(parents=True)
            (config.source_directory / "ready.mp4").write_bytes(b"video")
            collector_config = type(
                "Config",
                (),
                {
                    "youtube_config": config,
                    "output_path": lambda _self, name: root / name,
                },
            )()
            fake_client = FakeYoutubeClient()
            with patch("ui_helpers.create_youtube_client", return_value=fake_client):
                overview = load_youtube_overview(collector_config, include_channel=True)

        self.assertTrue(overview.token_reusable)
        self.assertEqual(overview.channel_name, "Existing channel")
        self.assertEqual(overview.pending_uploads, 1)


class YoutubeRunnerTests(unittest.TestCase):
    """Verify explicit YouTube CLI flags route without invoking another pipeline stage."""

    def test_upload_flags_route_one_and_all_to_the_existing_uploader_runner(self) -> None:
        with patch("run_pipeline.run_youtube_uploader", return_value=0) as uploader:
            self.assertEqual(run_pipeline_main(["--upload-youtube", "--all"]), 0)

        self.assertTrue(uploader.call_args.kwargs["process_all"])
        self.assertFalse(uploader.call_args.kwargs["upload_one"])

        with patch("run_pipeline.run_youtube_uploader", return_value=0) as uploader:
            self.assertEqual(run_pipeline_main(["--upload-youtube-one"]), 0)

        self.assertFalse(uploader.call_args.kwargs["process_all"])
        self.assertTrue(uploader.call_args.kwargs["upload_one"])

    def test_login_and_status_are_separate_non_upload_commands(self) -> None:
        with (
            patch("run_pipeline.run_youtube_login", return_value=0) as login,
            patch("run_pipeline.run_youtube_status", return_value=0) as status,
            patch("run_pipeline.run_youtube_uploader") as uploader,
        ):
            self.assertEqual(run_pipeline_main(["--youtube-login"]), 0)
            login.assert_called_once()
            status.assert_not_called()
            uploader.assert_not_called()

        with (
            patch("run_pipeline.run_youtube_login") as login,
            patch("run_pipeline.run_youtube_status", return_value=0) as status,
            patch("run_pipeline.run_youtube_uploader") as uploader,
        ):
            self.assertEqual(run_pipeline_main(["--youtube-status"]), 0)
            status.assert_called_once()
            login.assert_not_called()
            uploader.assert_not_called()

        self.assertEqual(
            run_pipeline_main(["--youtube-login", "--upload-youtube-one"]),
            2,
        )
