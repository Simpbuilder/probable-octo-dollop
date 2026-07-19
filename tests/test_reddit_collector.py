"""Offline tests for Reddit filtering, metadata collection, and resilience."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collector.collector import RedditMetadataCollector
from collector.models import CollectorConfig, SourceConfig
from collector.reddit_client import RedditCredentialsError, RedditSubredditUnavailableError, load_reddit_credentials
from collector.storage import load_all_clip_metadata


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakePost:
    """Minimal submission-like object used to avoid live Reddit calls in tests."""

    id: str = "abc123"
    title: str = "A test Reddit video"
    author: object | None = "test_author"
    score: int = 100
    num_comments: int = 10
    created_utc: float = NOW.timestamp()
    is_video: bool = True
    over_18: bool = False
    permalink: str = "/r/funny/comments/abc123/a_test_reddit_video/"
    media: object | None = None
    secure_media: object | None = None

    def __post_init__(self) -> None:
        """Give normal fake posts a Reddit-hosted video media object."""
        if self.media is None and self.is_video:
            self.media = {
                "reddit_video": {
                    "fallback_url": "https://v.redd.it/abc123/DASH_720.mp4",
                    "duration": 20,
                    "width": 720,
                    "height": 1280,
                }
            }


class FakeRedditClient:
    """Return configured listings or raise a configured per-subreddit error."""

    def __init__(self, listings: dict[str, object]) -> None:
        """Store the fake submission listings used by a test."""
        self.listings = listings
        self.calls: list[tuple[str, str, int, str]] = []

    def iter_submissions(
        self,
        subreddit_name: str,
        sorting_mode: str,
        posts_to_inspect: int,
        top_time_filter: str,
    ):
        """Yield a listing and preserve the selection arguments for assertions."""
        self.calls.append((subreddit_name, sorting_mode, posts_to_inspect, top_time_filter))
        result = self.listings[subreddit_name]
        if isinstance(result, Exception):
            raise result
        yield from result


def make_config(metadata_file: Path, subreddits: tuple[str, ...] = ("funny",)) -> CollectorConfig:
    """Build a small collector configuration backed by temporary metadata storage."""
    reddit = SourceConfig(
        name="reddit",
        enabled=True,
        subreddits=subreddits,
        minimum_score=50,
        maximum_clip_length_seconds=90,
        maximum_post_age_days=7,
        sorting_mode="top",
        top_time_filter="week",
        posts_to_inspect=5,
        allow_nsfw=False,
    )
    return CollectorConfig(
        source_configs={"reddit": reddit},
        output_folders={"metadata": metadata_file.parent},
        metadata_file=metadata_file,
    )


class RedditMetadataCollectorTests(unittest.TestCase):
    """Verify configured Reddit posts are handled without using the network."""

    def collect_one(self, post: FakePost):
        """Run one fake post through a temporary metadata store."""
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        metadata_file = Path(temporary_directory.name) / "clips.json"
        client = FakeRedditClient({"funny": [post]})
        summary = RedditMetadataCollector(
            make_config(metadata_file), client, clock=lambda: NOW
        ).collect()
        return summary, metadata_file, client

    def test_accepts_reddit_hosted_video_post(self) -> None:
        """An eligible Reddit-hosted video is persisted as complete metadata."""
        summary, metadata_file, client = self.collect_one(FakePost())

        clips = load_all_clip_metadata(metadata_file)
        self.assertEqual(summary.accepted, 1)
        self.assertEqual(summary.errors, 0)
        self.assertEqual(clips[0].source_post_id, "abc123")
        self.assertEqual(clips[0].local_file_path, None)
        self.assertEqual(clips[0].download_status, "pending")
        self.assertEqual(client.calls, [("funny", "top", 5, "week")])

    def test_rejects_non_video_post(self) -> None:
        """A normal link or text post does not enter metadata storage."""
        summary, metadata_file, _ = self.collect_one(FakePost(is_video=False))

        self.assertEqual(summary.rejected_by_filters, 1)
        self.assertEqual(load_all_clip_metadata(metadata_file), [])

    def test_rejects_low_score_post(self) -> None:
        """Posts below the configured minimum score are rejected."""
        summary, metadata_file, _ = self.collect_one(FakePost(score=49))

        self.assertEqual(summary.rejected_by_filters, 1)
        self.assertEqual(load_all_clip_metadata(metadata_file), [])

    def test_rejects_nsfw_post(self) -> None:
        """NSFW posts are rejected unless explicitly enabled in configuration."""
        summary, metadata_file, _ = self.collect_one(FakePost(over_18=True))

        self.assertEqual(summary.rejected_by_filters, 1)
        self.assertEqual(load_all_clip_metadata(metadata_file), [])

    def test_rejects_old_post(self) -> None:
        """Posts older than the configured maximum age are rejected."""
        old_timestamp = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp()
        summary, metadata_file, _ = self.collect_one(FakePost(created_utc=old_timestamp))

        self.assertEqual(summary.rejected_by_filters, 1)
        self.assertEqual(load_all_clip_metadata(metadata_file), [])

    def test_skips_duplicate_post(self) -> None:
        """A second pass over the same Reddit post is counted as a duplicate."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            config = make_config(metadata_file)
            client = FakeRedditClient({"funny": [FakePost()]})

            first_summary = RedditMetadataCollector(config, client, clock=lambda: NOW).collect()
            second_summary = RedditMetadataCollector(config, client, clock=lambda: NOW).collect()

            self.assertEqual(first_summary.accepted, 1)
            self.assertEqual(second_summary.duplicates, 1)
            self.assertEqual(len(load_all_clip_metadata(metadata_file)), 1)

    def test_subreddit_failure_does_not_stop_other_subreddits(self) -> None:
        """An inaccessible subreddit is logged while later subreddits still collect."""
        with TemporaryDirectory() as temporary_directory:
            metadata_file = Path(temporary_directory) / "clips.json"
            config = make_config(metadata_file, subreddits=("private", "funny"))
            client = FakeRedditClient(
                {
                    "private": RedditSubredditUnavailableError("r/private is unavailable."),
                    "funny": [FakePost()],
                }
            )

            summary = RedditMetadataCollector(config, client, clock=lambda: NOW).collect()

            self.assertEqual(summary.subreddits_checked, 2)
            self.assertEqual(summary.accepted, 1)
            self.assertEqual(summary.errors, 1)


class RedditCredentialTests(unittest.TestCase):
    """Verify absent credentials produce a clear preflight error without PRAW."""

    def test_missing_credentials_are_reported_cleanly(self) -> None:
        """No environment values results in a focused setup message."""
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(RedditCredentialsError, "REDDIT_CLIENT_ID"):
                load_reddit_credentials(Path(temporary_directory) / ".env", environ={})
