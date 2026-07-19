"""Orchestration for collecting and storing Reddit clip metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from .models import CollectorConfig, SourceConfig
from .reddit_client import (
    RedditAuthenticationError,
    RedditClient,
    RedditClientError,
    RedditSubredditUnavailableError,
)
from .reddit_filter import evaluate_reddit_post
from .reddit_metadata import create_reddit_clip_metadata
from .storage import DuplicateClipError, clip_exists, save_clip_metadata


@dataclass(slots=True)
class CollectionSummary:
    """Counters describing the outcome of one Reddit collector run."""

    subreddits_checked: int = 0
    posts_inspected: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected_by_filters: int = 0
    errors: int = 0
    authentication_failed: bool = False


class ClipCollector:
    """Base container for future source adapters and local collection workflows."""

    def __init__(self, config: CollectorConfig) -> None:
        """Keep validated configuration ready for a future collection run."""
        self.config = config


class RedditMetadataCollector(ClipCollector):
    """Collect suitable Reddit video metadata without downloading media files."""

    def __init__(
        self,
        config: CollectorConfig,
        reddit_client: RedditClient,
        *,
        clock: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up a collector with an injectable API client and clock for tests."""
        super().__init__(config)
        self._reddit_client = reddit_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._logger = logger or logging.getLogger(__name__)

    def collect(self) -> CollectionSummary:
        """Inspect configured subreddits and persist accepted video metadata."""
        summary = CollectionSummary()
        source_config = self.config.source_configs.get("reddit")
        if source_config is None or not source_config.enabled:
            self._logger.info("Reddit collection is disabled in config/sources.json.")
            return summary

        for subreddit_name in source_config.subreddits:
            summary.subreddits_checked += 1
            try:
                self._collect_subreddit(subreddit_name, source_config, summary)
            except RedditAuthenticationError as error:
                summary.errors += 1
                summary.authentication_failed = True
                self._logger.error("Reddit authentication failed: %s", error)
                break
            except RedditSubredditUnavailableError as error:
                summary.errors += 1
                self._logger.warning("Skipping subreddit: %s", error)
            except RedditClientError as error:
                summary.errors += 1
                self._logger.error("Reddit API error for r/%s: %s", subreddit_name, error)
            except Exception:
                summary.errors += 1
                self._logger.exception("Unexpected error while checking r/%s", subreddit_name)
        return summary

    def _collect_subreddit(
        self,
        subreddit_name: str,
        source_config: SourceConfig,
        summary: CollectionSummary,
    ) -> None:
        """Process one listing without allowing a bad post to stop that listing."""
        for post in self._reddit_client.iter_submissions(
            subreddit_name=subreddit_name,
            sorting_mode=source_config.sorting_mode,
            posts_to_inspect=source_config.posts_to_inspect,
            top_time_filter=source_config.top_time_filter,
        ):
            summary.posts_inspected += 1
            try:
                self._process_post(post, subreddit_name, source_config, summary)
            except Exception:
                summary.errors += 1
                self._logger.exception("Skipping an unprocessable post from r/%s", subreddit_name)

    def _process_post(
        self,
        post: object,
        subreddit_name: str,
        source_config: SourceConfig,
        summary: CollectionSummary,
    ) -> None:
        """Filter, build, deduplicate, and save one submission's metadata."""
        filter_result = evaluate_reddit_post(post, source_config, now=self._clock())
        if not filter_result.accepted:
            summary.rejected_by_filters += 1
            self._logger.debug("Rejected post from r/%s: %s", subreddit_name, filter_result.reason)
            return

        if filter_result.video is None:
            raise RuntimeError("Accepted post did not contain Reddit video details.")
        clip = create_reddit_clip_metadata(post, subreddit_name, filter_result.video)
        if clip_exists(self.config.metadata_file, clip):
            summary.duplicates += 1
            self._logger.debug("Skipped duplicate Reddit post: %s", clip.source_post_id)
            return

        try:
            save_clip_metadata(self.config.metadata_file, clip)
        except DuplicateClipError:
            summary.duplicates += 1
            self._logger.debug("Skipped duplicate Reddit post: %s", clip.source_post_id)
            return
        summary.accepted += 1
