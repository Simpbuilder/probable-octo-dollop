"""Run the configured Reddit metadata collector from the project root."""

from __future__ import annotations

import logging
from pathlib import Path

from collector import (
    CollectionSummary,
    ConfigurationError,
    RedditCredentialsError,
    RedditMetadataCollector,
    create_reddit_client,
    load_collector_config,
    load_reddit_credentials,
)
from collector.reddit_client import RedditClientError


def configure_logging() -> None:
    """Configure concise console logging for recoverable collector failures."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def print_summary(summary: CollectionSummary) -> None:
    """Print the stable terminal summary without exposing implementation details."""
    print(f"Subreddits checked: {summary.subreddits_checked}")
    print(f"Posts inspected: {summary.posts_inspected}")
    print(f"Accepted: {summary.accepted}")
    print(f"Duplicates: {summary.duplicates}")
    print(f"Rejected by filters: {summary.rejected_by_filters}")
    print(f"Errors: {summary.errors}")


def main() -> int:
    """Load configuration and credentials, then collect Reddit video metadata."""
    configure_logging()
    project_root = Path(__file__).resolve().parent

    try:
        config = load_collector_config(project_root / "config")
        credentials = load_reddit_credentials(project_root / ".env")
        reddit_client = create_reddit_client(credentials)
    except (ConfigurationError, RedditCredentialsError, RedditClientError) as error:
        print(f"Collector not started: {error}")
        return 2

    summary = RedditMetadataCollector(config, reddit_client).collect()
    print_summary(summary)
    return 1 if summary.authentication_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
