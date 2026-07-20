"""Offline tests for explicit Zernio Instagram draft and publish-now uploads."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from collector.models import ClipMetadata, InstagramConfig
from collector.storage import load_all_clip_metadata, save_clip_metadata
from publisher.history import append_post_history, build_post_history_record, load_post_history
from publisher.instagram_uploader import (
    InstagramAccountSelectionError,
    InstagramUploader,
    resolve_instagram_account,
)
from publisher.models import ZernioAccount, ZernioPostResult, ZernioPresignedMedia
from publisher.zernio_client import (
    ZernioClientError,
    ZernioCredentialsError,
    ZernioHttpClient,
    load_zernio_api_key,
)
from run_pipeline import main as run_pipeline_main, run_zernio_account_listing


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
CAPTION = (
    "Tonight, V stepped into the crowd, taking in live performances at Vogue World: Hollywood. "
    "Known for his own standout fashion moments, he kept it effortlessly stylish in a look "
    "worthy of the runway."
)
ACCOUNT = ZernioAccount(
    account_id="instagram-account",
    platform="instagram",
    username="vogue",
    display_name="Vogue",
    profile_id="profile-1",
)


class FakeResponse:
    """Minimal requests-like response with a fixed JSON payload."""

    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        """Store a JSON payload or a simulated HTTP failure."""
        self.payload = payload
        self.error = error
        self.status_code = 200

    def json(self) -> object:
        """Return the configured fake JSON response."""
        return self.payload

    def raise_for_status(self) -> None:
        """Raise the configured transport failure when requested."""
        if self.error is not None:
            raise self.error


class FakeSession:
    """Record HTTP calls while returning preconfigured Zernio endpoint responses."""

    def __init__(self) -> None:
        """Start with a conventional account, post list, and presign/post result."""
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.get_responses = [
            FakeResponse(
                {
                    "accounts": [
                        {
                            "_id": ACCOUNT.account_id,
                            "platform": "instagram",
                            "username": ACCOUNT.username,
                            "displayName": ACCOUNT.display_name,
                            "profileId": ACCOUNT.profile_id,
                            "isActive": True,
                        }
                    ]
                }
            ),
            FakeResponse({"posts": []}),
        ]
        self.post_responses = [
            FakeResponse(
                {
                    "uploadUrl": "https://storage.example/upload",
                    "publicUrl": "https://media.example/clip.mp4",
                }
            ),
            FakeResponse({"post": {"_id": "post-1", "status": "draft"}}),
        ]
        self.put_response = FakeResponse({})

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        """Record a GET request and return the next configured response."""
        self.calls.append(("GET", url, dict(kwargs)))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        """Record a POST request and return the next configured response."""
        self.calls.append(("POST", url, dict(kwargs)))
        return self.post_responses.pop(0)

    def put(self, url: str, **kwargs: object) -> FakeResponse:
        """Record a direct PUT request and return its configured response."""
        self.calls.append(("PUT", url, dict(kwargs)))
        return self.put_response


class FakeZernioClient:
    """In-memory Zernio client that never performs a network request."""

    def __init__(
        self,
        *,
        accounts: list[ZernioAccount] | None = None,
        remote_posts: list[dict[str, object]] | None = None,
        upload_failures: set[str] | None = None,
        post_failures: set[str] | None = None,
    ) -> None:
        """Configure deterministic account, remote-post, and file-specific failure behavior."""
        self.accounts = accounts if accounts is not None else [ACCOUNT]
        self.remote_posts = remote_posts or []
        self.upload_failures = upload_failures or set()
        self.post_failures = post_failures or set()
        self.presign_files: list[Path] = []
        self.uploaded_files: list[Path] = []
        self.post_calls: list[dict[str, object]] = []

    def list_accounts(self) -> list[ZernioAccount]:
        """Return the configured accounts."""
        return self.accounts

    def list_posts(self, account_id: str) -> list[dict[str, object]]:
        """Return the configured remote posts for duplicate testing."""
        self.last_post_lookup_account_id = account_id
        return self.remote_posts

    def request_presigned_media(self, video_file: Path) -> ZernioPresignedMedia:
        """Return a predictable media URL derived from the local filename."""
        self.presign_files.append(video_file)
        return ZernioPresignedMedia(
            upload_url=f"https://storage.example/{video_file.name}",
            public_url=f"https://media.example/{video_file.name}",
        )

    def upload_media(self, video_file: Path, media: ZernioPresignedMedia) -> None:
        """Record an upload or fail exactly the configured local file."""
        if video_file.name in self.upload_failures:
            raise ZernioClientError("Simulated upload failure")
        self.uploaded_files.append(video_file)

    def create_instagram_reel(
        self,
        *,
        account_id: str,
        public_media_url: str,
        filename: str,
        caption: str,
        publish_now: bool,
    ) -> ZernioPostResult:
        """Record exact post inputs or fail exactly the configured filename."""
        if filename in self.post_failures:
            raise ZernioClientError("Simulated post creation failure")
        self.post_calls.append(
            {
                "account_id": account_id,
                "public_media_url": public_media_url,
                "filename": filename,
                "caption": caption,
                "publish_now": publish_now,
            }
        )
        return ZernioPostResult(
            post_id=f"post-{filename}", status="published" if publish_now else "draft"
        )


def make_clip(unique_id: str, formatted_file: Path) -> ClipMetadata:
    """Build a downloaded, ready metadata entry matched to one hooked local MP4."""
    return ClipMetadata(
        unique_id=unique_id,
        source="manual",
        subreddit=None,
        source_post_id=unique_id,
        source_url=f"https://example.invalid/{unique_id}",
        title="Ready clip",
        author="manual",
        score=0,
        comment_count=0,
        created_at=NOW,
        media_url=None,
        local_file_path=formatted_file,
        download_status="downloaded",
        processing_status="ready",
        added_at=NOW,
        formatted_file_path=formatted_file,
    )


class ZernioHttpClientTests(unittest.TestCase):
    """Verify documented request shapes without a live Zernio account."""

    def test_lists_accounts_and_uses_presigned_reel_post_flow(self) -> None:
        """The client lists accounts, PUTs a presigned MP4, and creates an exact draft payload."""
        with TemporaryDirectory() as temporary_directory:
            video_file = Path(temporary_directory) / "clip.mp4"
            video_file.write_bytes(b"video")
            session = FakeSession()
            client = ZernioHttpClient(api_key="secret", session=session)

            accounts = client.list_accounts()
            posts = client.list_posts(ACCOUNT.account_id)
            media = client.request_presigned_media(video_file)
            client.upload_media(video_file, media)
            result = client.create_instagram_reel(
                account_id=ACCOUNT.account_id,
                public_media_url=media.public_url,
                filename=video_file.name,
                caption=CAPTION,
                publish_now=False,
            )

        self.assertEqual(accounts, [ACCOUNT])
        self.assertEqual(posts, [])
        self.assertEqual(result.status, "draft")
        self.assertEqual(session.calls[0][1], "https://zernio.com/api/v1/accounts")
        self.assertEqual(session.calls[1][2]["params"], {
            "platform": "instagram",
            "accountId": ACCOUNT.account_id,
            "limit": 100,
        })
        self.assertEqual(session.calls[2][2]["json"], {
            "filename": "clip.mp4",
            "contentType": "video/mp4",
            "size": 5,
        })
        self.assertEqual(session.calls[3][0], "PUT")
        self.assertNotIn("Authorization", session.calls[3][2]["headers"])
        payload = session.calls[4][2]["json"]
        self.assertEqual(payload["content"], CAPTION)
        self.assertFalse(payload["publishNow"])
        self.assertTrue(payload["isDraft"])
        self.assertEqual(payload["platforms"][0]["platformSpecificData"], {"contentType": "reels"})
        self.assertEqual(payload["mediaItems"][0]["url"], "https://media.example/clip.mp4")

    def test_publish_now_payload_is_explicit(self) -> None:
        """An immediate explicit command maps to publishNow true and isDraft false."""
        session = FakeSession()
        session.post_responses = [FakeResponse({"post": {"_id": "post-2", "status": "published"}})]
        client = ZernioHttpClient(api_key="secret", session=session)

        client.create_instagram_reel(
            account_id=ACCOUNT.account_id,
            public_media_url="https://media.example/clip.mp4",
            filename="clip.mp4",
            caption=CAPTION,
            publish_now=True,
        )

        payload = session.calls[0][2]["json"]
        self.assertTrue(payload["publishNow"])
        self.assertFalse(payload["isDraft"])

    def test_missing_api_key_has_a_clean_setup_error(self) -> None:
        """An explicit uploader command can report missing credentials without a traceback."""
        with self.assertRaises(ZernioCredentialsError):
            load_zernio_api_key(Path("missing.env"), environ={})


class InstagramUploaderTests(unittest.TestCase):
    """Verify local queue limits, duplicate safety, and independent file failures."""

    def make_environment(self, names: list[str], *, maximum_uploads: int = 1):
        """Create isolated hooked MP4s, metadata, and a enabled draft uploader configuration."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        source_directory = root / "clips" / "ready" / "hooked"
        source_directory.mkdir(parents=True)
        metadata_file = root / "metadata" / "clips.json"
        files: list[Path] = []
        for index, name in enumerate(names):
            video_file = source_directory / name
            video_file.write_bytes(b"video")
            files.append(video_file.resolve())
            save_clip_metadata(metadata_file, make_clip(f"clip-{index}", video_file.resolve()))
        config = InstagramConfig(
            enabled=True,
            account_id=ACCOUNT.account_id,
            source_directory=source_directory.resolve(),
            publish_mode="draft",
            default_caption=CAPTION,
            maximum_uploads_per_run=maximum_uploads,
            posted_directory=(root / "clips" / "posted").resolve(),
            duplicate_check_enabled=True,
        )
        return root, files, metadata_file, config

    def make_uploader(
        self,
        metadata_file: Path,
        root: Path,
        config: InstagramConfig,
        client: FakeZernioClient,
    ) -> InstagramUploader:
        """Build the production queue orchestrator with the local fake client."""
        return InstagramUploader(
            metadata_file=metadata_file,
            history_file=root / "metadata" / "zernio_post_history.json",
            config=config,
            client=client,
        )

    def test_single_account_is_selected_but_multiple_accounts_require_configuration(self) -> None:
        """The account resolver never makes an arbitrary choice among multiple Instagram accounts."""
        self.assertEqual(resolve_instagram_account([ACCOUNT], None), ACCOUNT)
        alternate = ZernioAccount(account_id="instagram-two", platform="instagram")
        with self.assertRaises(InstagramAccountSelectionError):
            resolve_instagram_account([ACCOUNT, alternate], None)
        with self.assertRaises(InstagramAccountSelectionError):
            resolve_instagram_account([], None)
        with self.assertRaises(InstagramAccountSelectionError):
            resolve_instagram_account([ACCOUNT], "wrong-account")

    def test_successful_draft_updates_history_and_clip_metadata_with_exact_caption(self) -> None:
        """A completed upload stores durable duplicate data and advances matching clip state."""
        root, files, metadata_file, config = self.make_environment(["clip.mp4"])
        client = FakeZernioClient()

        summary = self.make_uploader(metadata_file, root, config, client).run()

        self.assertEqual(summary.drafts, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(client.post_calls[0]["caption"], CAPTION)
        self.assertFalse(client.post_calls[0]["publish_now"])
        self.assertTrue(files[0].is_file())
        history = load_post_history(root / "metadata" / "zernio_post_history.json")
        self.assertEqual(history[0]["filename"], "clip.mp4")
        self.assertEqual(history[0]["publish_mode"], "draft")
        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(updated_clip.processing_status, "posted")
        self.assertEqual(updated_clip.formatted_file_path, files[0])

    def test_publish_now_override_creates_immediate_post(self) -> None:
        """A deliberate publish-now override changes only the per-command publish mode."""
        root, _, metadata_file, config = self.make_environment(["clip.mp4"])
        client = FakeZernioClient()

        summary = self.make_uploader(metadata_file, root, config, client).run(
            publish_now_override=True
        )

        self.assertEqual(summary.published, 1)
        self.assertTrue(client.post_calls[0]["publish_now"])
        history = load_post_history(root / "metadata" / "zernio_post_history.json")
        self.assertEqual(history[0]["publish_mode"], "publish_now")

    def test_local_history_and_remote_posts_prevent_duplicates(self) -> None:
        """Both durable local records and active remote posts suppress matching filenames."""
        root, files, metadata_file, config = self.make_environment(["already.mp4", "remote.mp4"])
        history_file = root / "metadata" / "zernio_post_history.json"
        append_post_history(
            history_file,
            build_post_history_record(
                post_id="old-post",
                status="draft",
                account_id=ACCOUNT.account_id,
                filename=files[0].name,
                public_media_url="https://media.example/already.mp4",
                publish_mode="draft",
            ),
        )
        client = FakeZernioClient(
            remote_posts=[
                {
                    "status": "published",
                    "platforms": [
                        {"platform": "instagram", "accountId": ACCOUNT.account_id}
                    ],
                    "mediaItems": [{"url": "https://media.example/remote.mp4"}],
                }
            ]
        )

        summary = self.make_uploader(metadata_file, root, config, client).run(process_all=True)

        self.assertEqual(summary.duplicates, 2)
        self.assertEqual(client.presign_files, [])

    def test_upload_and_post_failures_are_retryable_and_do_not_stop_later_files(self) -> None:
        """A bad upload or post increments failures while later finished Reels still continue."""
        root, _, metadata_file, config = self.make_environment(
            ["bad-upload.mp4", "bad-post.mp4", "good.mp4"], maximum_uploads=3
        )
        client = FakeZernioClient(
            upload_failures={"bad-upload.mp4"}, post_failures={"bad-post.mp4"}
        )

        summary = self.make_uploader(metadata_file, root, config, client).run()

        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.drafts, 1)
        self.assertEqual([call["filename"] for call in client.post_calls], ["good.mp4"])

    def test_limit_and_all_override_control_the_hooked_queue(self) -> None:
        """Normal runs respect the configured cap while --all maps to process_all."""
        root, _, metadata_file, config = self.make_environment(
            ["one.mp4", "two.mp4", "three.mp4"], maximum_uploads=1
        )
        limited_client = FakeZernioClient()
        limited = self.make_uploader(metadata_file, root, config, limited_client).run()
        self.assertEqual(limited.eligible, 3)
        self.assertEqual(limited.processing, 1)
        self.assertEqual(limited.remaining, 2)
        self.assertEqual(limited.drafts, 1)

        root, _, metadata_file, config = self.make_environment(
            ["one.mp4", "two.mp4", "three.mp4"], maximum_uploads=1
        )
        all_client = FakeZernioClient()
        all_summary = self.make_uploader(metadata_file, root, config, all_client).run(process_all=True)
        self.assertEqual(all_summary.processing, 3)
        self.assertEqual(all_summary.remaining, 0)
        self.assertEqual(all_summary.drafts, 3)

    def test_plain_ready_files_are_never_scanned_for_instagram_upload(self) -> None:
        """The uploader's source boundary excludes ready/plain and all other pipeline folders."""
        root, _, metadata_file, config = self.make_environment(["hooked.mp4"])
        plain_directory = root / "clips" / "ready" / "plain"
        plain_directory.mkdir(parents=True)
        (plain_directory / "plain.mp4").write_bytes(b"video")
        client = FakeZernioClient()

        summary = self.make_uploader(metadata_file, root, config, client).run(process_all=True)

        self.assertEqual(summary.found, 1)
        self.assertEqual([call["filename"] for call in client.post_calls], ["hooked.mp4"])

    def test_move_after_upload_uses_posted_directory_without_overwriting_source_first(self) -> None:
        """An opt-in move happens only after post history has a successful record."""
        root, files, metadata_file, config = self.make_environment(["clip.mp4"])
        config = replace(config, move_after_upload=True)
        client = FakeZernioClient()

        summary = self.make_uploader(metadata_file, root, config, client).run()

        destination = config.posted_directory / files[0].name
        self.assertEqual(summary.drafts, 1)
        self.assertFalse(files[0].exists())
        self.assertTrue(destination.is_file())
        updated_clip = load_all_clip_metadata(metadata_file)[0]
        self.assertEqual(updated_clip.formatted_file_path, destination.resolve())


class ZernioRunnerTests(unittest.TestCase):
    """Verify explicit CLI routing keeps uploads outside ordinary pipeline execution."""

    def test_zernio_cli_flags_route_to_explicit_commands(self) -> None:
        """Account listing and --all upload routing never start collector stages."""
        with patch("run_pipeline.load_collector_config") as load_config, patch(
            "run_pipeline.run_zernio_account_listing", return_value=0
        ) as account_listing, patch(
            "run_pipeline.run_instagram_uploader", return_value=0
        ) as uploader, patch("run_pipeline.run_manual_url_collector") as intake:
            load_config.return_value = object()
            self.assertEqual(run_pipeline_main(["--list-zernio-accounts"]), 0)
            self.assertEqual(run_pipeline_main(["--upload-instagram", "--all"]), 0)

        account_listing.assert_called_once()
        uploader.assert_called_once()
        self.assertTrue(uploader.call_args.kwargs["process_all"])
        intake.assert_not_called()

    def test_account_listing_output_shows_safe_account_details_only(self) -> None:
        """The listing exposes account selection values but never includes an API key."""
        output = StringIO()
        client = FakeZernioClient(accounts=[ACCOUNT])
        with (
            patch("run_pipeline.load_zernio_api_key", return_value="secret-value"),
            patch("run_pipeline.create_zernio_client", return_value=client),
            redirect_stdout(output),
        ):
            self.assertEqual(run_zernio_account_listing(Path("project")), 0)

        listing = output.getvalue()
        self.assertIn("Platform: instagram", listing)
        self.assertIn("Username: vogue", listing)
        self.assertIn("Display name: Vogue", listing)
        self.assertIn("Account ID: instagram-account", listing)
        self.assertIn("Profile ID: profile-1", listing)
        self.assertNotIn("secret-value", listing)
