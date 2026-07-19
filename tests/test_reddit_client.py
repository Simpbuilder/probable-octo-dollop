"""Offline tests for PRAW listing selection without importing PRAW itself."""

from __future__ import annotations

from collections.abc import Iterator
import unittest

from collector.reddit_client import PrawRedditClient


class FakeSubreddit:
    """Small PRAW-like subreddit object that records listing method calls."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[str, int, str | None]] = []

    def hot(self, *, limit: int) -> list[str]:
        """Return a deterministic hot listing."""
        self.calls.append(("hot", limit, None))
        return ["hot-post"]

    def new(self, *, limit: int) -> list[str]:
        """Return a deterministic new listing."""
        self.calls.append(("new", limit, None))
        return ["new-post"]

    def top(self, *, limit: int, time_filter: str) -> list[str]:
        """Return a deterministic top listing."""
        self.calls.append(("top", limit, time_filter))
        return ["top-post"]


class FakePrawReddit:
    """Provide one fake subreddit to the PRAW adapter."""

    def __init__(self, subreddit: FakeSubreddit) -> None:
        """Keep the supplied fake subreddit available by name."""
        self._subreddit = subreddit

    def subreddit(self, _name: str) -> FakeSubreddit:
        """Return the fake subreddit without making a network request."""
        return self._subreddit


class PrawRedditClientTests(unittest.TestCase):
    """Verify supported collector sorting modes map to PRAW methods correctly."""

    def test_selects_hot_new_and_top_listings(self) -> None:
        """Top passes its configured time filter while hot and new use a limit."""
        subreddit = FakeSubreddit()
        client = PrawRedditClient(FakePrawReddit(subreddit))

        expected = {
            "hot": ["hot-post"],
            "new": ["new-post"],
            "top": ["top-post"],
        }
        for sorting_mode, posts in expected.items():
            with self.subTest(sorting_mode=sorting_mode):
                listing: Iterator[object] = client.iter_submissions(
                    subreddit_name="funny",
                    sorting_mode=sorting_mode,
                    posts_to_inspect=7,
                    top_time_filter="month",
                )
                self.assertEqual(list(listing), posts)

        self.assertEqual(
            subreddit.calls,
            [("hot", 7, None), ("new", 7, None), ("top", 7, "month")],
        )
