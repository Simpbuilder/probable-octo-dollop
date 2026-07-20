"""Queue orchestration for explicit Zernio-backed Instagram Reel uploads."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import logging
import shutil

from collector.file_utils import ensure_path_is_within_directory
from collector.models import ClipMetadata, InstagramConfig
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .history import (
    build_post_history_record,
    append_post_history,
    history_has_duplicate,
    load_post_history,
    remote_posts_have_duplicate,
)
from .models import ZernioAccount
from .zernio_client import ZernioClientError, ZernioClientProtocol


class InstagramAccountSelectionError(ValueError):
    """Raised when no unambiguous active Instagram account is configured."""


@dataclass(slots=True)
class UploadSummary:
    """Counters displayed after a draft or publish-now Instagram upload pass."""

    found: int = 0
    eligible: int = 0
    processing: int = 0
    remaining: int = 0
    drafts: int = 0
    published: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0


def resolve_instagram_account(
    accounts: Iterable[ZernioAccount],
    configured_account_id: str | None,
) -> ZernioAccount:
    """Choose an active Instagram account without ever guessing between several choices."""
    instagram_accounts = [
        account
        for account in accounts
        if account.platform == "instagram" and account.active
    ]
    if configured_account_id is not None:
        normalized_id = configured_account_id.strip().casefold()
        for account in instagram_accounts:
            if account.account_id.casefold() == normalized_id:
                return account
        raise InstagramAccountSelectionError(
            "Configured Instagram account_id was not found among active Zernio accounts."
        )
    if len(instagram_accounts) == 1:
        return instagram_accounts[0]
    if not instagram_accounts:
        raise InstagramAccountSelectionError(
            "No active Instagram accounts were found in Zernio. Connect one first."
        )
    raise InstagramAccountSelectionError(
        "Multiple active Instagram accounts were found. Set account_id in config/instagram.json."
    )


class InstagramUploader:
    """Upload only hooked ready MP4 files while preserving local originals by default."""

    def __init__(
        self,
        *,
        metadata_file: Path,
        history_file: Path,
        config: InstagramConfig,
        client: ZernioClientProtocol,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up local state, strict source paths, and an injectable Zernio client."""
        self._metadata_file = Path(metadata_file)
        self._history_file = Path(history_file)
        self._config = config
        self._client = client
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        process_all: bool = False,
        maximum_uploads_override: int | None = None,
        publish_now_override: bool | None = None,
    ) -> UploadSummary:
        """Create Zernio draft or immediate Reel posts without stopping after one failure."""
        summary = UploadSummary()
        if not self._config.enabled:
            self._logger.error("Instagram uploads are disabled in config/instagram.json.")
            summary.failed = 1
            return summary
        try:
            account = resolve_instagram_account(
                self._client.list_accounts(), self._config.account_id
            )
            history = load_post_history(self._history_file)
        except (InstagramAccountSelectionError, ZernioClientError, ValueError) as error:
            self._logger.error("Instagram uploader could not start: %s", error)
            summary.failed = 1
            return summary
        if self._config.account_id is None:
            self._logger.info(
                "Using the only active Instagram account (%s); set account_id in "
                "config/instagram.json to save this selection.",
                account.account_id,
            )

        clips_by_path = self._load_clips_by_formatted_path()
        source_files = self._hooked_mp4_files()
        summary.found = len(source_files)
        remote_posts = self._load_remote_posts(account)
        eligible_files: list[Path] = []
        for video_file in source_files:
            if self._config.duplicate_check_enabled and (
                history_has_duplicate(history, video_file, account.account_id)
                or remote_posts_have_duplicate(remote_posts, video_file, account.account_id)
            ):
                summary.duplicates += 1
                continue
            eligible_files.append(video_file)

        summary.eligible = len(eligible_files)
        limit = maximum_uploads_override or self._config.maximum_uploads_per_run
        summary.processing = len(eligible_files) if process_all else min(len(eligible_files), limit)
        summary.remaining = summary.eligible - summary.processing
        publish_now = (
            publish_now_override
            if publish_now_override is not None
            else self._config.publish_mode == "publish_now"
        )
        for video_file in eligible_files[: summary.processing]:
            self._upload_one(
                video_file,
                account,
                clips_by_path,
                publish_now=publish_now,
                summary=summary,
            )
        return summary

    def _hooked_mp4_files(self) -> list[Path]:
        """List direct MP4 files in the configured hooked-only source directory."""
        source_directory = self._config.source_directory
        if not source_directory.is_dir():
            self._logger.warning("Instagram source directory does not exist: %s", source_directory)
            return []
        files: list[Path] = []
        for path in source_directory.iterdir():
            if not path.is_file() or path.suffix.casefold() != ".mp4":
                continue
            try:
                files.append(ensure_path_is_within_directory(path, source_directory))
            except ValueError:
                self._logger.warning("Skipping hooked Reel outside its source directory: %s", path)
        return sorted(files)

    def _load_remote_posts(self, account: ZernioAccount) -> list[dict[str, object]]:
        """Use remote posts for duplicate safety, falling back safely to local history on failure."""
        if not self._config.duplicate_check_enabled:
            return []
        try:
            return [dict(post) for post in self._client.list_posts(account.account_id)]
        except ZernioClientError as error:
            self._logger.warning(
                "Could not load remote Zernio posts; using local post history only: %s", error
            )
            return []

    def _load_clips_by_formatted_path(self) -> dict[Path, ClipMetadata]:
        """Load optional matching metadata so successful posts can advance local clip state."""
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except Exception as error:
            self._logger.warning("Could not load clip metadata for upload state updates: %s", error)
            return {}
        result: dict[Path, ClipMetadata] = {}
        for clip in clips:
            if clip.formatted_file_path is None:
                continue
            candidate = Path(clip.formatted_file_path)
            if not candidate.is_absolute():
                candidate = self._metadata_file.parent.parent / candidate
            result[candidate.resolve()] = clip
        return result

    def _upload_one(
        self,
        video_file: Path,
        account: ZernioAccount,
        clips_by_path: dict[Path, ClipMetadata],
        *,
        publish_now: bool,
        summary: UploadSummary,
    ) -> None:
        """Upload one local MP4, create its Reel post, then record durable local history."""
        try:
            media = self._client.request_presigned_media(video_file)
            self._client.upload_media(video_file, media)
            post = self._client.create_instagram_reel(
                account_id=account.account_id,
                public_media_url=media.public_url,
                filename=video_file.name,
                caption=self._config.default_caption,
                publish_now=publish_now,
            )
            append_post_history(
                self._history_file,
                build_post_history_record(
                    post_id=post.post_id,
                    status=post.status,
                    account_id=account.account_id,
                    filename=video_file.name,
                    public_media_url=media.public_url,
                    publish_mode="publish_now" if publish_now else "draft",
                ),
            )
            final_path = self._finalize_local_file(video_file)
            self._mark_matching_clip_posted(video_file, final_path, clips_by_path)
            if publish_now:
                summary.published += 1
            else:
                summary.drafts += 1
        except (ZernioClientError, OSError, ValueError) as error:
            summary.failed += 1
            self._logger.error("Instagram upload failed for %s: %s", video_file.name, error)
        except Exception as error:
            summary.failed += 1
            self._logger.exception("Unexpected Instagram upload failure for %s", video_file.name)

    def _finalize_local_file(self, video_file: Path) -> Path:
        """Optionally move or delete after the durable upload record is saved; default is unchanged."""
        if self._config.delete_after_upload:
            video_file.unlink()
            return video_file
        if not self._config.move_after_upload:
            return video_file
        destination_directory = self._config.posted_directory
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / video_file.name
        if destination.exists():
            raise OSError(f"Posted destination already exists: {destination}")
        shutil.move(str(video_file), str(destination))
        return destination.resolve()

    def _mark_matching_clip_posted(
        self,
        source_file: Path,
        final_path: Path,
        clips_by_path: dict[Path, ClipMetadata],
    ) -> None:
        """Advance matching clip metadata after Zernio success without changing source download data."""
        clip = clips_by_path.get(source_file.resolve())
        if clip is None:
            return
        try:
            update_clip_metadata(
                self._metadata_file,
                replace(
                    clip,
                    processing_status="posted",
                    formatted_file_path=final_path if not self._config.delete_after_upload else None,
                ),
            )
        except Exception as error:
            self._logger.error("Could not update posted metadata for %s: %s", clip.unique_id, error)
