"""Run configured manual URL and Reddit metadata collectors from the project root."""

from __future__ import annotations

import logging
from pathlib import Path

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
    """Configure concise console logging for recoverable collector failures."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


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


def main() -> int:
    """Load configuration and run the manual, Reddit, or combined pipeline mode."""
    configure_logging()
    project_root = Path(__file__).resolve().parent
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        print(f"Pipeline not started: {error}")
        return 2

    exit_code = 0
    for collector_name in selected_collectors(config.pipeline_mode):
        if collector_name == "manual_urls":
            exit_code = max(exit_code, run_manual_url_collector(config, project_root))
        else:
            exit_code = max(exit_code, run_reddit_api_collector(config, project_root))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
