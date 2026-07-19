"""Pure filtering and Reddit-hosted video extraction for candidate posts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import SourceConfig


class MalformedRedditPostError(ValueError):
    """Raised when a required post field cannot be safely interpreted."""


@dataclass(frozen=True, slots=True)
class RedditVideoDetails:
    """Video data Reddit exposes before a clip is downloaded."""

    media_url: str
    duration_seconds: float | None
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The result of applying configured collection rules to one post."""

    accepted: bool
    reason: str | None = None
    video: RedditVideoDetails | None = None


def evaluate_reddit_post(
    post: object,
    source_config: SourceConfig,
    now: datetime | None = None,
) -> FilterResult:
    """Accept only suitable Reddit-hosted video posts for the configured source."""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("The filtering clock must be timezone-aware.")

    if bool(getattr(post, "over_18", False)) and not source_config.allow_nsfw:
        return FilterResult(accepted=False, reason="nsfw")

    score = _required_int_attribute(post, "score")
    if score < source_config.minimum_score:
        return FilterResult(accepted=False, reason="low_score")

    created_at = _created_at(post)
    oldest_allowed = current_time - timedelta(days=source_config.maximum_post_age_days)
    if created_at < oldest_allowed:
        return FilterResult(accepted=False, reason="too_old")

    if not bool(getattr(post, "is_video", False)):
        return FilterResult(accepted=False, reason="not_video")

    video = _extract_reddit_video(post)
    if video is None:
        return FilterResult(accepted=False, reason="not_reddit_hosted_video")
    if (
        video.duration_seconds is not None
        and video.duration_seconds > source_config.maximum_clip_length_seconds
    ):
        return FilterResult(accepted=False, reason="too_long")
    return FilterResult(accepted=True, video=video)


def _extract_reddit_video(post: object) -> RedditVideoDetails | None:
    """Return ``reddit_video`` data only; external video embeds are excluded."""
    media_candidates = (getattr(post, "media", None), getattr(post, "secure_media", None))
    for media in media_candidates:
        if not isinstance(media, Mapping):
            continue
        reddit_video = media.get("reddit_video")
        if not isinstance(reddit_video, Mapping):
            continue

        media_url = _first_string(reddit_video, "fallback_url", "dash_url")
        if media_url is None:
            continue
        return RedditVideoDetails(
            media_url=media_url,
            duration_seconds=_optional_positive_number(reddit_video.get("duration")),
            width=_optional_positive_int(reddit_video.get("width")),
            height=_optional_positive_int(reddit_video.get("height")),
        )
    return None


def _created_at(post: object) -> datetime:
    """Convert Reddit's UTC timestamp into a timezone-aware datetime."""
    raw_timestamp = getattr(post, "created_utc", None)
    if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)):
        raise MalformedRedditPostError("Post has no valid created_utc timestamp.")
    try:
        return datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise MalformedRedditPostError("Post has an invalid created_utc timestamp.") from error


def _required_int_attribute(post: object, name: str) -> int:
    """Read an integer submission attribute without accepting booleans."""
    value = getattr(post, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRedditPostError(f"Post has no valid {name} value.")
    return value


def _first_string(data: Mapping[str, Any], *names: str) -> str | None:
    """Return the first present non-empty string in a Reddit media object."""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _optional_positive_number(value: object) -> float | None:
    """Normalize an optional positive JSON number, otherwise return ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _optional_positive_int(value: object) -> int | None:
    """Normalize an optional positive integer, otherwise return ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
