"""Conversion from a filtered Reddit submission to pipeline metadata."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import ClipMetadata
from .reddit_filter import MalformedRedditPostError, RedditVideoDetails


def create_reddit_clip_metadata(
    post: object,
    subreddit_name: str,
    video: RedditVideoDetails,
    added_at: datetime | None = None,
) -> ClipMetadata:
    """Create pipeline metadata from a post that already passed filtering."""
    post_id = _required_string_attribute(post, "id")
    title = _required_string_attribute(post, "title")
    author = _author_name(post)
    source_url = _source_url(post, post_id)
    created_at = _created_at(post)

    return ClipMetadata(
        unique_id=f"reddit-{post_id}",
        source="reddit",
        subreddit=subreddit_name,
        source_post_id=post_id,
        source_url=source_url,
        title=title,
        author=author,
        score=_required_int_attribute(post, "score"),
        comment_count=_required_int_attribute(post, "num_comments"),
        created_at=created_at,
        media_url=video.media_url,
        local_file_path=None,
        duration_seconds=video.duration_seconds,
        width=video.width,
        height=video.height,
        download_status="pending",
        processing_status="pending",
        added_at=added_at or datetime.now(timezone.utc),
    )


def _source_url(post: object, post_id: str) -> str:
    """Normalize Reddit's relative permalink or use a stable post URL fallback."""
    permalink = getattr(post, "permalink", None)
    if isinstance(permalink, str) and permalink.strip():
        if permalink.startswith(("http://", "https://")):
            return permalink
        return f"https://www.reddit.com{permalink}"
    return f"https://www.reddit.com/comments/{post_id}/"


def _author_name(post: object) -> str:
    """Return a post author name while treating deleted authors as invalid posts."""
    author = getattr(post, "author", None)
    if author is None:
        raise MalformedRedditPostError("Post author is deleted or unavailable.")
    author_name = str(author).strip()
    if not author_name:
        raise MalformedRedditPostError("Post author is empty or unavailable.")
    return author_name


def _required_string_attribute(post: object, name: str) -> str:
    """Read a non-empty string submission attribute."""
    value = getattr(post, name, None)
    if not isinstance(value, str) or not value.strip():
        raise MalformedRedditPostError(f"Post has no valid {name} value.")
    return value


def _required_int_attribute(post: object, name: str) -> int:
    """Read a required integer submission attribute without accepting booleans."""
    value = getattr(post, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRedditPostError(f"Post has no valid {name} value.")
    return value


def _created_at(post: object) -> datetime:
    """Convert a valid Reddit UTC timestamp into a metadata timestamp."""
    timestamp = getattr(post, "created_utc", None)
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise MalformedRedditPostError("Post has no valid created_utc timestamp.")
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise MalformedRedditPostError("Post has an invalid created_utc timestamp.") from error
