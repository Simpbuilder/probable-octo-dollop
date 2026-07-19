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


def print_download_summary(summary: DownloadSummary) -> None:
    """Print the stable terminal summary for a pending-media download pass."""
    print("Download queue")
    print(f"Pending: {summary.pending}")
    print(f"Downloaded: {summary.downloaded}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")


def print_format_summary(summary: FormatSummary) -> None:
    """Print the stable terminal summary for a vertical formatting pass."""
    print("Vertical formatter")
    print(f"Pending: {summary.pending}")
    print(f"Formatted: {summary.formatted}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")


def run_manual_url_collector(config: CollectorConfig, project_root: Path) -> int:
    """Run the local URL queue without requiring Reddit credentials or network access."""
    summary = ManualUrlCollector(
        input_file=project_root / "input_urls.txt",
        processed_file=config.output_path("metadata") / "processed_urls.txt",
        metadata_file=config.metadata_file,
    ).collect()
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


def should_run_collectors(explicit_download: bool, explicit_format: bool) -> bool:
    """Keep ``--format`` focused on existing downloads while combined runs collect first."""
    return not explicit_format or explicit_download


def run_pending_clip_downloader(config: CollectorConfig) -> int:
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
        ).run()
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
        )
    except FfmpegDependencyError as error:
        print(f"Formatter not started: {error}")
        return 2
    except FfmpegClientError as error:
        print(f"Formatter not started: {error}")
        return 2
    print_format_summary(summary)
    return 1 if summary.failed else 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Load configuration and run the requested collection, download, and formatting stages."""
    configure_logging()
    parsed_arguments = parse_arguments(arguments)
    if parsed_arguments.hook is not None and not parsed_arguments.format_one:
        print("Pipeline not started: --hook requires --format-one.")
        return 2
    if parsed_arguments.hook is not None and not parsed_arguments.hook.strip():
        print("Pipeline not started: --hook must not be blank.")
        return 2
    project_root = Path(__file__).resolve().parent
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        print(f"Pipeline not started: {error}")
        return 2

    format_requested = parsed_arguments.format or parsed_arguments.format_one
    exit_code = 0
    if should_run_collectors(parsed_arguments.download, format_requested):
        for collector_name in selected_collectors(config.pipeline_mode):
            if collector_name == "manual_urls":
                exit_code = max(exit_code, run_manual_url_collector(config, project_root))
            else:
                exit_code = max(exit_code, run_reddit_api_collector(config, project_root))
    if should_run_downloader(
        config,
        parsed_arguments.download,
        format_only=format_requested and not parsed_arguments.download,
    ):
        exit_code = max(exit_code, run_pending_clip_downloader(config))
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
            ),
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
