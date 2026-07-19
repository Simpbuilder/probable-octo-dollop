"""PRAW-backed Reddit API access and credential handling."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class RedditClientError(RuntimeError):
    """Base error raised while communicating with Reddit through PRAW."""


class RedditCredentialsError(RedditClientError):
    """Raised when required Reddit credentials are not available."""


class RedditDependencyError(RedditClientError):
    """Raised when an optional runtime dependency has not been installed."""


class RedditAuthenticationError(RedditClientError):
    """Raised when Reddit rejects the configured API credentials."""


class RedditSubredditUnavailableError(RedditClientError):
    """Raised when a subreddit is private, banned, missing, or inaccessible."""


class RedditNetworkError(RedditClientError):
    """Raised when Reddit cannot be reached or returns a transient API failure."""


@dataclass(frozen=True, slots=True)
class RedditCredentials:
    """Read-only Reddit application credentials loaded from the environment."""

    client_id: str
    client_secret: str
    user_agent: str


class RedditClient(Protocol):
    """Minimal Reddit listing interface consumed by the collector."""

    def iter_submissions(
        self,
        subreddit_name: str,
        sorting_mode: str,
        posts_to_inspect: int,
        top_time_filter: str,
    ) -> Iterator[object]:
        """Yield a configured number of submissions for one subreddit."""


def load_reddit_credentials(
    env_path: Path,
    environ: Mapping[str, str] | None = None,
) -> RedditCredentials:
    """Load required credentials from the root ``.env`` file and environment.

    Existing environment variables take precedence so credentials can also be
    provided by a deployment environment without changing the project files.
    """
    env_path = Path(env_path)
    if env_path.exists():
        try:
            from dotenv import load_dotenv
        except ModuleNotFoundError as error:
            raise RedditDependencyError(
                "python-dotenv is not installed. Run: pip install -r requirements.txt"
            ) from error
        load_dotenv(dotenv_path=env_path, override=False)

    environment = os.environ if environ is None else environ
    values = {
        "REDDIT_CLIENT_ID": environment.get("REDDIT_CLIENT_ID", "").strip(),
        "REDDIT_CLIENT_SECRET": environment.get("REDDIT_CLIENT_SECRET", "").strip(),
        "REDDIT_USER_AGENT": environment.get("REDDIT_USER_AGENT", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        names = ", ".join(missing)
        raise RedditCredentialsError(
            f"Missing Reddit credentials: {names}. Create .env from .env.example."
        )

    return RedditCredentials(
        client_id=values["REDDIT_CLIENT_ID"],
        client_secret=values["REDDIT_CLIENT_SECRET"],
        user_agent=values["REDDIT_USER_AGENT"],
    )


def create_reddit_client(credentials: RedditCredentials) -> "PrawRedditClient":
    """Create a read-only PRAW client from already-validated credentials."""
    try:
        import praw
    except ModuleNotFoundError as error:
        raise RedditDependencyError(
            "praw is not installed. Run: pip install -r requirements.txt"
        ) from error

    try:
        reddit = praw.Reddit(
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            user_agent=credentials.user_agent,
        )
        reddit.read_only = True
    except Exception as error:  # PRAW can reject malformed local configuration here.
        raise RedditAuthenticationError("Could not initialize the Reddit API client.") from error
    return PrawRedditClient(reddit)


class PrawRedditClient:
    """Adapter that keeps PRAW details out of collector orchestration."""

    def __init__(self, reddit: Any) -> None:
        """Wrap an initialized PRAW ``Reddit`` instance."""
        self._reddit = reddit

    def iter_submissions(
        self,
        subreddit_name: str,
        sorting_mode: str,
        posts_to_inspect: int,
        top_time_filter: str,
    ) -> Iterator[object]:
        """Yield submissions using Reddit's hot, new, or top listing endpoints."""
        try:
            subreddit = self._reddit.subreddit(subreddit_name)
            if sorting_mode == "hot":
                listing = subreddit.hot(limit=posts_to_inspect)
            elif sorting_mode == "new":
                listing = subreddit.new(limit=posts_to_inspect)
            elif sorting_mode == "top":
                listing = subreddit.top(limit=posts_to_inspect, time_filter=top_time_filter)
            else:
                raise ValueError(f"Unsupported Reddit sorting mode: {sorting_mode}")

            yield from listing
        except RedditClientError:
            raise
        except Exception as error:
            raise _translate_praw_error(error, subreddit_name) from error


def _translate_praw_error(error: Exception, subreddit_name: str) -> RedditClientError:
    """Turn PRAW and prawcore errors into collector-level error categories."""
    error_name = type(error).__name__
    message = str(error).lower()
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)

    if (
        error_name == "OAuthException"
        or status_code == 401
        or "invalid client" in message
        or "invalid token" in message
    ):
        return RedditAuthenticationError(
            "Reddit rejected the API credentials. Verify REDDIT_CLIENT_ID, "
            "REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT."
        )
    if error_name in {"Forbidden", "NotFound", "Redirect"} or status_code in {403, 404}:
        return RedditSubredditUnavailableError(
            f"r/{subreddit_name} is unavailable, private, banned, or inaccessible."
        )
    if error_name in {"RequestException", "ResponseException", "ServerError"}:
        return RedditNetworkError(f"Could not retrieve posts from r/{subreddit_name}.")
    return RedditClientError(f"Could not retrieve posts from r/{subreddit_name}.")
