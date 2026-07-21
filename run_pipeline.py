"""Run configured collectors, downloads, and optional vertical Reel formatting."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import replace
import logging
from pathlib import Path

from downloader import (
    PendingClipDownloader,
    YtDlpClientError,
    YtDlpDependencyError,
    create_yt_dlp_client,
)
from downloader.models import DownloadSummary
from formatter import (
    FfmpegClient,
    FfmpegClientError,
    FfmpegDependencyError,
    PendingClipFormatter,
)
from formatter.models import FormatSummary
from hook_generator import (
    HookGenerationClientError,
    HookGenerationSummary,
    PendingHookGenerator,
    create_openai_hook_client,
    inspect_hook_flow,
    load_openai_api_key,
)
from publisher import (
    InstagramUploader,
    UploadProgressCallback,
    UploadSummary,
    YoutubeClientError,
    YoutubeUploadSummary,
    YoutubeUploader,
    ZernioClientError,
    create_zernio_client,
    create_youtube_client,
    count_pending_youtube_uploads,
    login_to_youtube,
    load_zernio_api_key,
)
from pipeline_runtime import QueueProgress, QueueProgressCallback
from cleanup import run_cleanup_command
from archive import ArchiveManager
from archive.recreation import ClipRecreationService

from collector import (
    CollectionSummary,
    ConfigurationError,
    ManualUrlCollector,
    ManualUrlSummary,
    RedditCredentialsError,
    RedditMetadataCollector,
    create_reddit_client,
    load_collector_config,
    load_reddit_credentials,
)
from collector.models import ClipMetadata, CollectorConfig, PipelineMode
from collector.storage import load_all_clip_metadata
from collector.reddit_client import RedditClientError


def configure_logging() -> None:
    """Configure concise console logging for recoverable pipeline failures."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit runtime controls without changing the saved pipeline mode."""
    parser = argparse.ArgumentParser(
        description="Collect clip metadata, download media, and format vertical ready clips."
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download pending clips after configured collectors finish.",
    )
    parser.add_argument(
        "--format",
        action="store_true",
        help="Format already-downloaded pending clips into vertical ready MP4 files.",
    )
    parser.add_argument(
        "--format-one",
        action="store_true",
        help="Format one pending download, or validate one ready clip when used with --hook.",
    )
    parser.add_argument(
        "--hook",
        help="Use this manual hook text with --format-one without overwriting a prior ready render.",
    )
    parser.add_argument(
        "--generate-hooks",
        action="store_true",
        help="Generate three hook candidates from existing clip metadata without rendering video.",
    )
    parser.add_argument(
        "--force-hooks",
        action="store_true",
        help="Regenerate candidates even when a clip already has saved hook candidates.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every eligible queue item instead of the configured per-run safety limit.",
    )
    parser.add_argument(
        "--debug-hook-flow",
        metavar="CLIP_ID",
        help="Print saved hook values and the formatter choice for one clip without changing data.",
    )
    parser.add_argument(
        "--list-zernio-accounts",
        action="store_true",
        help="List connected Zernio accounts without uploading or creating a post.",
    )
    parser.add_argument(
        "--upload-instagram",
        action="store_true",
        help="Upload eligible hooked ready MP4 files to Zernio using the configured draft mode.",
    )
    parser.add_argument(
        "--upload-one-instagram",
        action="store_true",
        help="Upload one eligible hooked ready MP4 file to Zernio using the configured draft mode.",
    )
    parser.add_argument(
        "--publish-now",
        action="store_true",
        help="Publish an explicit Instagram upload immediately instead of creating a draft.",
    )
    parser.add_argument(
        "--post-delay",
        type=int,
        metavar="SECONDS",
        help="Override Instagram spacing between successful posts for this upload run.",
    )
    parser.add_argument(
        "--upload-youtube",
        action="store_true",
        help="Upload eligible hooked ready MP4 files to the configured YouTube channel.",
    )
    parser.add_argument(
        "--upload-youtube-one",
        action="store_true",
        help="Upload one eligible hooked ready MP4 file to the configured YouTube channel.",
    )
    parser.add_argument(
        "--youtube-status",
        action="store_true",
        help="Show reusable YouTube credential, channel, source, and pending-upload status.",
    )
    parser.add_argument(
        "--youtube-login",
        action="store_true",
        help="Open Google OAuth in a browser and save a root-level reusable YouTube token.",
    )
    parser.add_argument(
        "--archive-missing",
        action="store_true",
        help="Copy eligible hooked ready videos missing from the permanent local archive.",
    )
    parser.add_argument(
        "--verify-archive",
        action="store_true",
        help="Read and verify permanent archive records without changing video files.",
    )
    parser.add_argument(
        "--list-recreatable",
        action="store_true",
        help="List clips that can be explicitly recreated without uploading anything.",
    )
    parser.add_argument(
        "--recreate",
        metavar="CLIP_ID",
        help="Recreate one hooked ready video from its saved metadata and source media.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --recreate to overwrite its existing routed ready output.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove only clearly temporary pipeline files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview cleanup operations without changing files.",
    )
    parser.add_argument(
        "--all-temporary",
        action="store_true",
        help="With --cleanup, also remove regeneratable pending and ready media after confirmation.",
    )
    parser.add_argument(
        "--reset-project",
        action="store_true",
        help="Reset a batch after typing RESET; preserves credentials, config, uploads, and posted videos.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation for --cleanup --all-temporary or one explicit --recreate. It never bypasses RESET.",
    )
    return parser.parse_args(arguments)


def selected_collectors(pipeline_mode: PipelineMode) -> tuple[str, ...]:
    """Return the collector names enabled by a validated pipeline mode."""
    modes = {
        "reddit_api": ("reddit_api",),
        "manual_urls": ("manual_urls",),
        "both": ("manual_urls", "reddit_api"),
    }
    return modes[pipeline_mode]


def print_reddit_summary(summary: CollectionSummary) -> None:
    """Print the stable summary for a Reddit API collection run."""
    print("Reddit API collector")
    print(f"Subreddits checked: {summary.subreddits_checked}")
    print(f"Posts inspected: {summary.posts_inspected}")
    print(f"Accepted: {summary.accepted}")
    print(f"Duplicates: {summary.duplicates}")
    print(f"Rejected by filters: {summary.rejected_by_filters}")
    print(f"Errors: {summary.errors}")


def print_manual_url_summary(summary: ManualUrlSummary) -> None:
    """Print the stable summary for one manual URL queue intake run."""
    print("Manual URL intake")
    print(f"URLs found: {summary.urls_found}")
    print(f"Accepted: {summary.accepted}")
    print(f"Duplicates: {summary.duplicates}")
    print(f"Invalid URLs: {summary.invalid_urls}")
    print(f"Errors: {summary.errors}")
    _print_queue_limit_notice(summary)


def print_download_summary(summary: DownloadSummary) -> None:
    """Print the stable terminal summary for a pending-media download pass."""
    print("Download queue")
    print(f"Pending: {summary.pending}")
    print(f"Downloaded: {summary.downloaded}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    _print_queue_limit_notice(summary)


def print_format_summary(summary: FormatSummary) -> None:
    """Print the stable terminal summary for a vertical formatting pass."""
    print("Vertical formatter")
    print(f"Pending: {summary.pending}")
    print(f"Formatted: {summary.formatted}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    _print_queue_limit_notice(summary)


def print_hook_generation_summary(summary: HookGenerationSummary) -> None:
    """Print the stable terminal summary for one metadata-only hook generation pass."""
    print("Hook generation")
    print(f"Pending: {summary.pending}")
    print(f"Generated: {summary.generated}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    _print_queue_limit_notice(summary)


def print_instagram_upload_summary(summary: UploadSummary) -> None:
    """Print the stable terminal summary for an explicit Instagram upload pass."""
    print("Instagram upload queue")
    print(f"Found hooked MP4 files: {summary.found}")
    print(f"Drafts created: {summary.drafts}")
    print(f"Published now: {summary.published}")
    print(f"Duplicates: {summary.duplicates}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    _print_queue_limit_notice(summary)


def print_youtube_upload_summary(summary: YoutubeUploadSummary) -> None:
    """Print a stable, safe summary after an explicit YouTube upload pass."""
    print("YouTube Shorts upload")
    print(f"Found: {summary.found}")
    print(f"Eligible: {summary.eligible}")
    print(f"Uploaded: {summary.uploaded}")
    print(f"Duplicates: {summary.duplicates}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    _print_queue_limit_notice(summary)


def _print_queue_limit_notice(summary: object) -> None:
    """Explain deferred work only when a configured stage limit left items untouched."""
    remaining = getattr(summary, "remaining", 0)
    if remaining <= 0:
        return
    print(f"Eligible: {getattr(summary, 'eligible')}")
    print(f"Processing: {getattr(summary, 'processing')}")
    print(f"Remaining: {remaining}")
    print("Run again or use --all")


def print_hook_flow_debug(
    config: CollectorConfig,
    clip_id: str,
    *,
    manual_hook: str | None = None,
) -> int:
    """Print one clip's persisted candidates and pure formatter resolution without side effects."""
    if config.formatter_config is None:
        print("Hook flow debug not started: formatter configuration is missing.")
        return 2
    try:
        debug = inspect_hook_flow(
            config.metadata_file,
            clip_id,
            config.formatter_config.hook,
            manual_hook=manual_hook,
        )
    except (KeyError, ValueError) as error:
        print(f"Hook flow debug not started: {error}")
        return 2

    print("Hook flow debug")
    print(f"Metadata file: {debug.metadata_file}")
    print(f"Clip ID: {debug.clip_id}")
    print("Hook candidates:")
    if debug.hook_candidates:
        for index, candidate in enumerate(debug.hook_candidates, start=1):
            print(f"{index}. {candidate}")
    else:
        print("(none)")
    print(f"Selected hook: {debug.selected_hook if debug.selected_hook is not None else '(none)'}")
    if debug.selection is None:
        print("Final hook for rendering: (none)")
        print("Reason/source: no explicit hook, selected_hook, hook_text, or enabled title fallback")
    else:
        print(f"Final hook for rendering: {debug.selection.text}")
        print(f"Reason/source: {debug.selection.reason} ({debug.selection.source})")
    return 0


def run_manual_url_collector(
    config: CollectorConfig,
    project_root: Path,
    *,
    process_all: bool = False,
) -> int:
    """Run the local URL queue without requiring Reddit credentials or network access."""
    summary = ManualUrlCollector(
        input_file=project_root / "input_urls.txt",
        processed_file=config.output_path("metadata") / "processed_urls.txt",
        metadata_file=config.metadata_file,
        maximum_urls_per_run=config.manual_urls_per_run,
    ).collect(process_all=process_all)
    print_manual_url_summary(summary)
    return 1 if summary.errors else 0


def run_reddit_api_collector(config: CollectorConfig, project_root: Path) -> int:
    """Run the existing PRAW collector and report setup failures without a traceback."""
    try:
        credentials = load_reddit_credentials(project_root / ".env")
        reddit_client = create_reddit_client(credentials)
    except (RedditCredentialsError, RedditClientError) as error:
        print(f"Reddit collector not started: {error}")
        return 2

    summary = RedditMetadataCollector(config, reddit_client).collect()
    print_reddit_summary(summary)
    return 1 if summary.authentication_failed else 0


def should_run_downloader(
    config: CollectorConfig,
    explicit_download: bool,
    *,
    format_only: bool = False,
) -> bool:
    """Require an explicit flag unless the saved downloader setting intentionally enables it."""
    return explicit_download or (not format_only and bool(
        config.downloader_config is not None and config.downloader_config.enabled
    ))


def should_run_formatter(config: CollectorConfig, explicit_format: bool) -> bool:
    """Require an explicit flag unless the saved formatter setting intentionally enables it."""
    return explicit_format or bool(
        config.formatter_config is not None and config.formatter_config.enabled
    )


def should_run_hook_generation(config: CollectorConfig, explicit_generation: bool) -> bool:
    """Allow explicit generation while keeping automatic API usage disabled by default."""
    return explicit_generation or bool(
        config.hook_generation_config is not None and config.hook_generation_config.enabled
    )


def should_run_collectors(explicit_download: bool, explicit_format: bool) -> bool:
    """Keep ``--format`` focused on existing downloads while combined runs collect first."""
    return not explicit_format or explicit_download


def run_pending_clip_downloader(
    config: CollectorConfig,
    *,
    process_all: bool = False,
    progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Run the yt-dlp downloader and report missing dependencies without a traceback."""
    if config.downloader_config is None:
        print("Downloader not started: downloader configuration is missing.")
        return 2
    try:
        media_client = create_yt_dlp_client()
    except YtDlpDependencyError as error:
        print(f"Downloader not started: {error}")
        return 2

    try:
        summary = PendingClipDownloader(
            metadata_file=config.metadata_file,
            config=config.downloader_config,
            media_client=media_client,
    ).run(process_all=process_all, progress_callback=progress_callback)
    except YtDlpClientError as error:
        print(f"Downloader not started: {error}")
        return 2
    print_download_summary(summary)
    return 1 if summary.failed else 0


def run_pending_clip_formatter(
    config: CollectorConfig,
    *,
    maximum_clips_override: int | None = None,
    manual_hook: str | None = None,
    include_ready_for_manual_hook: bool = False,
    process_all: bool = False,
    clip_ids: frozenset[str] | None = None,
    progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Run the FFmpeg formatter and report missing local tools without a traceback."""
    if config.formatter_config is None:
        print("Formatter not started: formatter configuration is missing.")
        return 2

    formatter_config = config.formatter_config
    if maximum_clips_override is not None:
        formatter_config = replace(
            formatter_config,
            maximum_clips_per_run=maximum_clips_override,
        )

    try:
        archive_manager = _archive_manager(config)
        summary = PendingClipFormatter(
            metadata_file=config.metadata_file,
            config=formatter_config,
            ffmpeg_client=FfmpegClient(),
            archive_manager=archive_manager,
        ).run(
            manual_hook=manual_hook,
            include_ready_for_manual_hook=include_ready_for_manual_hook,
            process_all=process_all,
            clip_ids=clip_ids,
            progress_callback=progress_callback,
        )
    except FfmpegDependencyError as error:
        print(f"Formatter not started: {error}")
        return 2
    except FfmpegClientError as error:
        print(f"Formatter not started: {error}")
        return 2
    print_format_summary(summary)
    return 1 if summary.failed else 0


def _archive_manager(config: CollectorConfig, *, overwrite_existing: bool = False) -> ArchiveManager | None:
    """Build the optional archive service only when all existing local paths are configured."""
    if config.archive_config is None or config.formatter_config is None:
        return None
    return ArchiveManager(
        metadata_file=config.metadata_file,
        ready_directory=config.formatter_config.output_directory,
        config=replace(config.archive_config, overwrite_existing=overwrite_existing),
    )


def run_archive_missing(config: CollectorConfig) -> int:
    """Repair only absent hooked archive copies without rendering, downloading, or uploading."""
    manager = _archive_manager(config)
    if manager is None:
        print("Archive command not started: config/archive.json or formatter configuration is missing.")
        return 2
    summary = manager.archive_missing()
    print("Hooked video archive")
    print(f"Eligible: {summary.eligible}")
    print(f"Archived: {summary.archived}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    return 1 if summary.failed else 0


def run_verify_archive(config: CollectorConfig) -> int:
    """Report archive integrity findings without mutating video or metadata files."""
    manager = _archive_manager(config)
    if manager is None:
        print("Archive command not started: config/archive.json or formatter configuration is missing.")
        return 2
    verification = manager.verify_archive()
    print("Hooked video archive verification")
    print(f"Checked: {verification.checked}")
    print(f"Verified: {verification.verified}")
    print(f"Missing: {verification.missing}")
    print(f"Mismatched: {verification.mismatched}")
    print(f"Untracked files: {verification.untracked_files}")
    for finding in verification.findings:
        print(f"- {finding}")
    return 1 if verification.missing or verification.mismatched else 0


def list_recreatable_clips(config: CollectorConfig) -> int:
    """Print one local-only list of known clips that retain source URL metadata for recreation."""
    if config.formatter_config is None:
        print("Recreatable clips unavailable: formatter configuration is missing.")
        return 2
    clips = [clip for clip in load_all_clip_metadata(config.metadata_file) if _is_recreatable(clip)]
    print("Recreatable clips")
    if not clips:
        print("(none)")
        return 0
    for clip in clips:
        source_state = "source available" if _source_file_exists(clip) else "re-download required"
        archive_state = clip.archive_status or "not archived"
        print(f"{clip.unique_id}: {source_state}; archive {archive_state}; {clip.title}")
    return 0


def print_recreation_preview(config: CollectorConfig, clip_id: str) -> int:
    """Show exact local metadata used for an explicit recreation before requesting confirmation."""
    clip = next(
        (candidate for candidate in load_all_clip_metadata(config.metadata_file) if candidate.unique_id == clip_id),
        None,
    )
    if clip is None:
        print(f"Recreation not started: clip metadata was not found: {clip_id}")
        return 2
    print("Recreation preview")
    print(f"Clip ID: {clip.unique_id}")
    print(f"Title: {clip.title}")
    print(f"Selected hook: {clip.selected_hook or clip.hook_text or '(none)'}")
    print(f"Source URL: {clip.source_url}")
    print(f"Source file exists: {'yes' if _source_file_exists(clip) else 'no'}")
    print(f"Archive path: {clip.archive_path or '(none)'}")
    return 0 if _is_recreatable(clip) else 2


def _source_file_exists(clip: ClipMetadata) -> bool:
    """Treat only a real stored local source as reusable; absent paths trigger the normal downloader."""
    return clip.local_file_path is not None and Path(clip.local_file_path).is_file()


def _is_recreatable(clip: ClipMetadata) -> bool:
    """Keep automatic work impossible: a recreation needs saved hook data and a recoverable source."""
    return bool((clip.selected_hook or clip.hook_text) and (clip.source_url.strip() or _source_file_exists(clip)))


def run_clip_recreation(config: CollectorConfig, clip_id: str, *, force: bool) -> int:
    """Recreate exactly one clip through existing local media services and never upload it."""
    if config.downloader_config is None or config.formatter_config is None:
        print("Recreation not started: downloader or formatter configuration is missing.")
        return 2
    clip = next(
        (candidate for candidate in load_all_clip_metadata(config.metadata_file) if candidate.unique_id == clip_id),
        None,
    )
    if clip is None or not _is_recreatable(clip):
        print("Recreation not started: the clip needs a selected/manual hook and a source URL or local source file.")
        return 2
    manager = _archive_manager(config, overwrite_existing=force)
    if manager is None:
        print("Recreation not started: config/archive.json is missing.")
        return 2
    try:
        downloader = PendingClipDownloader(
            metadata_file=config.metadata_file,
            config=config.downloader_config,
            media_client=create_yt_dlp_client(),
        )
        formatter = PendingClipFormatter(
            metadata_file=config.metadata_file,
            config=replace(config.formatter_config, overwrite=force),
            ffmpeg_client=FfmpegClient(),
            archive_manager=manager,
        )
        result = ClipRecreationService(
            config.metadata_file,
            downloader,
            formatter,
            manager,
        ).recreate(clip_id, force=force)
    except (YtDlpDependencyError, FfmpegDependencyError, FfmpegClientError, YtDlpClientError) as error:
        print(f"Recreation not started: {error}")
        return 2
    if result.archived:
        print(f"Recreated and archived: {clip_id}")
        print(f"Ready output: {result.archive_path}")
        return 0
    print(f"Recreation failed for {clip_id}: {result.error or 'unknown error'}")
    return 1


def run_pending_hook_generator(
    config: CollectorConfig,
    project_root: Path,
    *,
    force: bool = False,
    process_all: bool = False,
    progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Generate hook candidates from metadata without starting any media formatting stage."""
    if config.hook_generation_config is None:
        print("Hook generation not started: hook generation configuration is missing.")
        return 2
    try:
        api_key = load_openai_api_key(project_root / ".env")
        client = create_openai_hook_client(api_key)
    except HookGenerationClientError as error:
        print(f"Hook generation not started: {error}")
        return 2

    summary = PendingHookGenerator(
        metadata_file=config.metadata_file,
        config=config.hook_generation_config,
        client=client,
    ).run(force=force, process_all=process_all, progress_callback=progress_callback)
    print_hook_generation_summary(summary)
    return 1 if summary.failed else 0


def run_zernio_account_listing(project_root: Path) -> int:
    """List safe connected-account details without writing data or exposing credentials."""
    try:
        client = create_zernio_client(load_zernio_api_key(project_root / ".env"))
        accounts = client.list_accounts()
    except ZernioClientError as error:
        print(f"Zernio account listing not started: {error}")
        return 2

    print("Zernio connected accounts")
    if not accounts:
        print("No connected accounts found.")
        return 0
    for account in accounts:
        display_name = account.display_name or "(not provided)"
        username = account.username or "(not provided)"
        profile_id = account.profile_id or "(not provided)"
        print(f"Platform: {account.platform}")
        print(f"Username: {username}")
        print(f"Display name: {display_name}")
        print(f"Account ID: {account.account_id}")
        print(f"Profile ID: {profile_id}")
        print(f"Status: {'active' if account.active else 'inactive'}")
    return 0


def run_instagram_uploader(
    config: CollectorConfig,
    project_root: Path,
    *,
    upload_one: bool,
    process_all: bool,
    publish_now: bool,
    post_delay: int | None = None,
    progress_callback: UploadProgressCallback | None = None,
    queue_progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Run the explicit hooked-Reel uploader without adding it to normal pipeline execution."""
    if config.instagram_config is None:
        print("Instagram uploader not started: config/instagram.json is missing.")
        return 2
    if not config.instagram_config.enabled:
        print("Instagram uploader not started: set enabled to true in config/instagram.json.")
        return 2
    try:
        client = create_zernio_client(load_zernio_api_key(project_root / ".env"))
    except ZernioClientError as error:
        print(f"Instagram uploader not started: {error}")
        return 2

    def relay_upload_progress(update: object) -> bool:
        """Retain the existing upload callback while optionally exposing generic job status."""
        continue_upload = True
        if progress_callback is not None:
            continue_upload = progress_callback(update) is not False
        if queue_progress_callback is not None:
            current_file = getattr(update, "current_file", None)
            successful_posts = int(getattr(update, "successful_posts", 0))
            total_posts = int(getattr(update, "total_posts", 0))
            failed_count = int(getattr(update, "failed_count", 0))
            phase = str(getattr(update, "phase", "uploading"))
            message = "Waiting before the next upload." if phase == "waiting" else "Uploading Reel."
            continue_upload = queue_progress_callback(
                QueueProgress(
                    stage="Publish Instagram" if publish_now else "Upload Instagram drafts",
                    current_item=current_file.name if current_file is not None else None,
                    completed_count=successful_posts,
                    total_count=total_posts,
                    failed_count=failed_count,
                    remaining_count=int(getattr(update, "remaining_posts", 0)),
                    message=message,
                )
            ) is not False and continue_upload
        return continue_upload

    summary = InstagramUploader(
        metadata_file=config.metadata_file,
        history_file=config.output_path("metadata") / "zernio_post_history.json",
        config=config.instagram_config,
        client=client,
    ).run(
        process_all=process_all,
        maximum_uploads_override=1 if upload_one else None,
        publish_now_override=True if publish_now else None,
        post_delay_override=post_delay,
        progress_callback=(relay_upload_progress if progress_callback or queue_progress_callback else None),
    )
    print_instagram_upload_summary(summary)
    return 1 if summary.failed else 0


def run_youtube_status(config: CollectorConfig) -> int:
    """Print read-only YouTube readiness without revealing credentials, tokens, or secret paths."""
    if config.youtube_config is None:
        print("YouTube status unavailable: config/youtube.json is missing.")
        return 2
    youtube = config.youtube_config
    status = create_youtube_client(youtube).authentication_status(include_channel=True)
    print("YouTube status")
    print(f"Credentials available: {'yes' if status.credentials_available else 'no'}")
    print(f"Reusable token available: {'yes' if status.token_available else 'no'}")
    print(f"Reusable token valid: {'yes' if status.token_reusable else 'no'}")
    print(f"Channel name: {status.channel.channel_name if status.channel else '(unavailable)'}")
    print(f"Channel ID: {status.channel.channel_id if status.channel else '(unavailable)'}")
    print(f"Configured source directory: {youtube.source_directory}")
    print(
        "Pending upload count: "
        f"{count_pending_youtube_uploads(history_file=config.output_path('metadata') / 'youtube_upload_history.json', config=youtube)}"
    )
    if status.error:
        print(f"Status detail: {status.error}")
    return 0


def run_youtube_login(config: CollectorConfig, project_root: Path) -> int:
    """Run explicit browser OAuth without inspecting or uploading any media."""
    if config.youtube_config is None:
        print("YouTube login unavailable: config/youtube.json is missing.")
        return 2
    client_secret_file = Path(project_root).resolve() / "client_secret.json"
    token_file = Path(project_root).resolve() / "token.json"
    print("Opening Google login in your browser...")
    try:
        channel = login_to_youtube(client_secret_file, token_file)
    except YoutubeClientError as error:
        print(f"YouTube login failed: {error}")
        return 2
    print("YouTube login complete")
    print(f"Channel name: {channel.channel_name}")
    print(f"Channel ID: {channel.channel_id}")
    return 0


def run_youtube_uploader(
    config: CollectorConfig,
    *,
    upload_one: bool,
    process_all: bool,
    queue_progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Run the explicit hooked-Short uploader without changing Instagram or normal pipeline behavior."""
    if config.youtube_config is None:
        print("YouTube uploader not started: config/youtube.json is missing.")
        return 2
    if not config.youtube_config.enabled:
        print("YouTube uploader not started: set enabled to true in config/youtube.json.")
        return 2

    def relay_progress(update: object) -> bool:
        """Translate the reusable YouTube progress type into the existing worker progress type."""
        if queue_progress_callback is None:
            return True
        current_file = getattr(update, "current_file", None)
        phase = str(getattr(update, "phase", "uploading"))
        message = "Waiting before the next YouTube upload." if phase == "waiting" else "Uploading YouTube Short."
        return queue_progress_callback(
            QueueProgress(
                stage="Upload YouTube Shorts",
                current_item=current_file.name if current_file is not None else None,
                completed_count=int(getattr(update, "uploaded_count", 0)),
                total_count=int(getattr(update, "total_uploads", 0)),
                failed_count=int(getattr(update, "failed_count", 0)),
                remaining_count=int(getattr(update, "remaining_uploads", 0)),
                message=message,
            )
        ) is not False

    summary = YoutubeUploader(
        metadata_file=config.metadata_file,
        history_file=config.output_path("metadata") / "youtube_upload_history.json",
        config=config.youtube_config,
        client=create_youtube_client(config.youtube_config),
    ).run(
        process_all=process_all,
        maximum_uploads_override=1 if upload_one else None,
        progress_callback=relay_progress if queue_progress_callback is not None else None,
    )
    print_youtube_upload_summary(summary)
    return 1 if summary.failed else 0


def _has_pipeline_stage_arguments(arguments: argparse.Namespace) -> bool:
    """Return whether cleanup was combined with another command that changes pipeline state."""
    return any(
        (
            arguments.download,
            arguments.format,
            arguments.format_one,
            arguments.generate_hooks,
            arguments.debug_hook_flow is not None,
            arguments.list_zernio_accounts,
            arguments.upload_instagram,
            arguments.upload_one_instagram,
            arguments.publish_now,
            arguments.upload_youtube,
            arguments.upload_youtube_one,
            arguments.youtube_status,
            arguments.youtube_login,
            arguments.archive_missing,
            arguments.verify_archive,
            arguments.list_recreatable,
            arguments.recreate is not None,
        )
    )


def main(
    arguments: Sequence[str] | None = None,
    *,
    progress_callback: QueueProgressCallback | None = None,
) -> int:
    """Load configuration and run the requested collection, download, and formatting stages."""
    configure_logging()
    parsed_arguments = parse_arguments(arguments)
    cleanup_requested = parsed_arguments.cleanup or parsed_arguments.reset_project
    if parsed_arguments.all_temporary and not parsed_arguments.cleanup:
        print("Pipeline not started: --all-temporary requires --cleanup.")
        return 2
    if parsed_arguments.dry_run and not cleanup_requested:
        print("Pipeline not started: --dry-run requires --cleanup or --reset-project.")
        return 2
    if parsed_arguments.yes and not (
        parsed_arguments.all_temporary or parsed_arguments.recreate is not None
    ):
        print("Pipeline not started: --yes requires --cleanup --all-temporary or --recreate.")
        return 2
    if parsed_arguments.reset_project and (
        parsed_arguments.cleanup or parsed_arguments.all_temporary or parsed_arguments.yes
    ):
        print("Pipeline not started: --reset-project cannot be combined with cleanup flags.")
        return 2
    if cleanup_requested and _has_pipeline_stage_arguments(parsed_arguments):
        print("Pipeline not started: cleanup commands must run separately from pipeline stages.")
        return 2
    archive_command_requested = any(
        (
            parsed_arguments.archive_missing,
            parsed_arguments.verify_archive,
            parsed_arguments.list_recreatable,
            parsed_arguments.recreate is not None,
        )
    )
    if parsed_arguments.force and parsed_arguments.recreate is None:
        print("Pipeline not started: --force requires --recreate CLIP_ID.")
        return 2
    if parsed_arguments.recreate is not None and not parsed_arguments.recreate.strip():
        print("Pipeline not started: --recreate requires a clip ID.")
        return 2
    if archive_command_requested and any(
        (
            parsed_arguments.download,
            parsed_arguments.format,
            parsed_arguments.format_one,
            parsed_arguments.generate_hooks,
            parsed_arguments.upload_instagram,
            parsed_arguments.upload_one_instagram,
            parsed_arguments.upload_youtube,
            parsed_arguments.upload_youtube_one,
            parsed_arguments.youtube_status,
            parsed_arguments.youtube_login,
        )
    ):
        print("Pipeline not started: archive commands must run separately from pipeline stages.")
        return 2
    if parsed_arguments.hook is not None and not parsed_arguments.format_one:
        print("Pipeline not started: --hook requires --format-one.")
        return 2
    if parsed_arguments.hook is not None and not parsed_arguments.hook.strip():
        print("Pipeline not started: --hook must not be blank.")
        return 2
    if parsed_arguments.force_hooks and not parsed_arguments.generate_hooks:
        print("Pipeline not started: --force-hooks requires --generate-hooks.")
        return 2
    if parsed_arguments.all and parsed_arguments.format_one:
        print("Pipeline not started: --all cannot be combined with --format-one.")
        return 2
    if parsed_arguments.upload_instagram and parsed_arguments.upload_one_instagram:
        print("Pipeline not started: choose --upload-instagram or --upload-one-instagram.")
        return 2
    if parsed_arguments.all and parsed_arguments.upload_one_instagram:
        print("Pipeline not started: --all cannot be combined with --upload-one-instagram.")
        return 2
    upload_requested = parsed_arguments.upload_instagram or parsed_arguments.upload_one_instagram
    youtube_upload_requested = parsed_arguments.upload_youtube or parsed_arguments.upload_youtube_one
    if parsed_arguments.upload_youtube and parsed_arguments.upload_youtube_one:
        print("Pipeline not started: choose --upload-youtube or --upload-youtube-one.")
        return 2
    if parsed_arguments.all and parsed_arguments.upload_youtube_one:
        print("Pipeline not started: --all cannot be combined with --upload-youtube-one.")
        return 2
    if parsed_arguments.youtube_status and youtube_upload_requested:
        print("Pipeline not started: --youtube-status cannot be combined with a YouTube upload command.")
        return 2
    if parsed_arguments.youtube_login and (
        parsed_arguments.youtube_status or youtube_upload_requested
    ):
        print("Pipeline not started: --youtube-login must run as a separate YouTube command.")
        return 2
    youtube_command_requested = (
        youtube_upload_requested
        or parsed_arguments.youtube_status
        or parsed_arguments.youtube_login
    )
    if youtube_command_requested and (
        upload_requested or parsed_arguments.list_zernio_accounts
    ):
        print("Pipeline not started: YouTube and Zernio publishing commands must run separately.")
        return 2
    if parsed_arguments.publish_now and not upload_requested:
        print("Pipeline not started: --publish-now requires an Instagram upload command.")
        return 2
    if parsed_arguments.post_delay is not None and not upload_requested:
        print("Pipeline not started: --post-delay requires an Instagram upload command.")
        return 2
    if parsed_arguments.post_delay is not None and parsed_arguments.post_delay < 0:
        print("Pipeline not started: --post-delay must be zero or greater.")
        return 2
    if parsed_arguments.list_zernio_accounts and upload_requested:
        print("Pipeline not started: --list-zernio-accounts cannot be combined with an upload command.")
        return 2
    if (parsed_arguments.list_zernio_accounts or upload_requested or youtube_command_requested) and any(
        (
            parsed_arguments.download,
            parsed_arguments.format,
            parsed_arguments.format_one,
            parsed_arguments.generate_hooks,
            parsed_arguments.debug_hook_flow is not None,
        )
    ):
        print("Pipeline not started: publishing commands must run separately from collection stages.")
        return 2
    project_root = Path(__file__).resolve().parent
    if cleanup_requested:
        return run_cleanup_command(
            project_root,
            all_temporary=parsed_arguments.all_temporary,
            reset_project=parsed_arguments.reset_project,
            dry_run=parsed_arguments.dry_run,
            yes=parsed_arguments.yes,
        )
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        print(f"Pipeline not started: {error}")
        return 2

    if parsed_arguments.list_zernio_accounts:
        return run_zernio_account_listing(project_root)
    if parsed_arguments.youtube_login:
        return run_youtube_login(config, project_root)
    if parsed_arguments.youtube_status:
        return run_youtube_status(config)
    if parsed_arguments.archive_missing:
        return run_archive_missing(config)
    if parsed_arguments.verify_archive:
        return run_verify_archive(config)
    if parsed_arguments.list_recreatable:
        return list_recreatable_clips(config)
    if parsed_arguments.recreate is not None:
        preview_exit_code = print_recreation_preview(config, parsed_arguments.recreate)
        if preview_exit_code:
            return preview_exit_code
        if not parsed_arguments.yes:
            confirmation = input(
                f"Recreate hooked video '{parsed_arguments.recreate}' from saved local metadata? Type RECREATE: "
            )
            if confirmation != "RECREATE":
                print("Recreation canceled. Type RECREATE exactly to continue.")
                return 2
        return run_clip_recreation(config, parsed_arguments.recreate, force=parsed_arguments.force)
    if upload_requested:
        return run_instagram_uploader(
            config,
            project_root,
            upload_one=parsed_arguments.upload_one_instagram,
            process_all=parsed_arguments.all,
            publish_now=parsed_arguments.publish_now,
            post_delay=parsed_arguments.post_delay,
            queue_progress_callback=progress_callback,
        )
    if youtube_upload_requested:
        return run_youtube_uploader(
            config,
            upload_one=parsed_arguments.upload_youtube_one,
            process_all=parsed_arguments.all,
            queue_progress_callback=progress_callback,
        )

    if parsed_arguments.debug_hook_flow is not None:
        return print_hook_flow_debug(
            config,
            parsed_arguments.debug_hook_flow,
            manual_hook=parsed_arguments.hook,
        )

    format_requested = parsed_arguments.format or parsed_arguments.format_one
    if parsed_arguments.generate_hooks and not (
        parsed_arguments.download or format_requested
    ):
        generator_kwargs = {
            "force": parsed_arguments.force_hooks,
            "process_all": parsed_arguments.all,
        }
        if progress_callback is not None:
            generator_kwargs["progress_callback"] = progress_callback
        return run_pending_hook_generator(config, project_root, **generator_kwargs)

    exit_code = 0
    if should_run_collectors(parsed_arguments.download, format_requested):
        for collector_name in selected_collectors(config.pipeline_mode):
            if collector_name == "manual_urls":
                exit_code = max(
                    exit_code,
                    run_manual_url_collector(config, project_root, process_all=parsed_arguments.all),
                )
            else:
                exit_code = max(exit_code, run_reddit_api_collector(config, project_root))
    if should_run_downloader(
        config,
        parsed_arguments.download,
        format_only=format_requested and not parsed_arguments.download,
    ):
        downloader_kwargs = {"process_all": parsed_arguments.all}
        if progress_callback is not None:
            downloader_kwargs["progress_callback"] = progress_callback
        exit_code = max(exit_code, run_pending_clip_downloader(config, **downloader_kwargs))
    if parsed_arguments.generate_hooks or (
        not format_requested and should_run_hook_generation(config, explicit_generation=False)
    ):
        generator_kwargs = {
            "force": parsed_arguments.force_hooks,
            "process_all": parsed_arguments.all,
        }
        if progress_callback is not None:
            generator_kwargs["progress_callback"] = progress_callback
        exit_code = max(
            exit_code, run_pending_hook_generator(config, project_root, **generator_kwargs)
        )
    if should_run_formatter(config, format_requested):
        formatter_kwargs = {
            "maximum_clips_override": 1 if parsed_arguments.format_one else None,
            "manual_hook": parsed_arguments.hook,
            "include_ready_for_manual_hook": (
                parsed_arguments.format_one and parsed_arguments.hook is not None
            ),
            "process_all": parsed_arguments.all,
        }
        if progress_callback is not None:
            formatter_kwargs["progress_callback"] = progress_callback
        exit_code = max(exit_code, run_pending_clip_formatter(config, **formatter_kwargs))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
