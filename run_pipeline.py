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
    UploadSummary,
    ZernioClientError,
    create_zernio_client,
    load_zernio_api_key,
)
from cleanup import run_cleanup_command

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
from collector.models import CollectorConfig, PipelineMode
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
        help="Skip the YES prompt for --cleanup --all-temporary. It never bypasses RESET.",
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


def run_pending_clip_downloader(config: CollectorConfig, *, process_all: bool = False) -> int:
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
    ).run(process_all=process_all)
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
        summary = PendingClipFormatter(
            metadata_file=config.metadata_file,
            config=formatter_config,
            ffmpeg_client=FfmpegClient(),
        ).run(
            manual_hook=manual_hook,
            include_ready_for_manual_hook=include_ready_for_manual_hook,
            process_all=process_all,
        )
    except FfmpegDependencyError as error:
        print(f"Formatter not started: {error}")
        return 2
    except FfmpegClientError as error:
        print(f"Formatter not started: {error}")
        return 2
    print_format_summary(summary)
    return 1 if summary.failed else 0


def run_pending_hook_generator(
    config: CollectorConfig,
    project_root: Path,
    *,
    force: bool = False,
    process_all: bool = False,
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
    ).run(force=force, process_all=process_all)
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

    summary = InstagramUploader(
        metadata_file=config.metadata_file,
        history_file=config.output_path("metadata") / "zernio_post_history.json",
        config=config.instagram_config,
        client=client,
    ).run(
        process_all=process_all,
        maximum_uploads_override=1 if upload_one else None,
        publish_now_override=True if publish_now else None,
    )
    print_instagram_upload_summary(summary)
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
        )
    )


def main(arguments: Sequence[str] | None = None) -> int:
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
    if parsed_arguments.yes and not parsed_arguments.all_temporary:
        print("Pipeline not started: --yes requires --cleanup --all-temporary.")
        return 2
    if parsed_arguments.reset_project and (
        parsed_arguments.cleanup or parsed_arguments.all_temporary or parsed_arguments.yes
    ):
        print("Pipeline not started: --reset-project cannot be combined with cleanup flags.")
        return 2
    if cleanup_requested and _has_pipeline_stage_arguments(parsed_arguments):
        print("Pipeline not started: cleanup commands must run separately from pipeline stages.")
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
    if parsed_arguments.publish_now and not upload_requested:
        print("Pipeline not started: --publish-now requires an Instagram upload command.")
        return 2
    if parsed_arguments.list_zernio_accounts and upload_requested:
        print("Pipeline not started: --list-zernio-accounts cannot be combined with an upload command.")
        return 2
    if (parsed_arguments.list_zernio_accounts or upload_requested) and any(
        (
            parsed_arguments.download,
            parsed_arguments.format,
            parsed_arguments.format_one,
            parsed_arguments.generate_hooks,
            parsed_arguments.debug_hook_flow is not None,
        )
    ):
        print("Pipeline not started: Zernio commands must run separately from collection stages.")
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
    if upload_requested:
        return run_instagram_uploader(
            config,
            project_root,
            upload_one=parsed_arguments.upload_one_instagram,
            process_all=parsed_arguments.all,
            publish_now=parsed_arguments.publish_now,
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
        return run_pending_hook_generator(
            config,
            project_root,
            force=parsed_arguments.force_hooks,
            process_all=parsed_arguments.all,
        )

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
        exit_code = max(
            exit_code,
            run_pending_clip_downloader(config, process_all=parsed_arguments.all),
        )
    if parsed_arguments.generate_hooks or (
        not format_requested and should_run_hook_generation(config, explicit_generation=False)
    ):
        exit_code = max(
            exit_code,
            run_pending_hook_generator(
                config,
                project_root,
                force=parsed_arguments.force_hooks,
                process_all=parsed_arguments.all,
            ),
        )
    if should_run_formatter(config, format_requested):
        exit_code = max(
            exit_code,
            run_pending_clip_formatter(
                config,
                maximum_clips_override=1 if parsed_arguments.format_one else None,
                manual_hook=parsed_arguments.hook,
                include_ready_for_manual_hook=(
                    parsed_arguments.format_one and parsed_arguments.hook is not None
                ),
                process_all=parsed_arguments.all,
            ),
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
