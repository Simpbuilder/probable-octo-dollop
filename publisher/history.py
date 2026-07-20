"""Durable local Zernio post-history storage and conservative duplicate matching."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ACTIVE_POST_STATUSES = frozenset(
    {"draft", "scheduled", "pending", "publishing", "published", "completed"}
)


def load_post_history(history_file: Path) -> list[dict[str, object]]:
    """Load local upload records, returning an empty history before the first upload."""
    history_file = Path(history_file)
    if not history_file.exists():
        return []
    try:
        raw_data = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read Zernio post history: {error}") from error

    records: object
    if isinstance(raw_data, dict):
        records = raw_data.get("posts")
    else:
        records = raw_data
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError("Zernio post history must contain a list of post records.")
    return [dict(record) for record in records]


def append_post_history(history_file: Path, record: Mapping[str, object]) -> None:
    """Append one successful or pending Zernio post record using an atomic local write."""
    records = load_post_history(history_file)
    post_id = _string_value(record.get("post_id"))
    if post_id and any(_string_value(item.get("post_id")) == post_id for item in records):
        return
    records.append(dict(record))
    payload = {"schema_version": 1, "posts": records}
    history_file = Path(history_file)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = history_file.with_suffix(f"{history_file.suffix}.tmp")
    temporary_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_file.replace(history_file)


def build_post_history_record(
    *,
    post_id: str,
    status: str,
    account_id: str,
    filename: str,
    public_media_url: str,
    publish_mode: str,
) -> dict[str, object]:
    """Build the self-contained record used to prevent later duplicate Reel posts."""
    return {
        "post_id": post_id,
        "status": status,
        "platform": "instagram",
        "account_id": account_id,
        "filename": filename,
        "filename_stem": Path(filename).stem,
        "public_media_url": public_media_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "publish_mode": publish_mode,
    }


def history_has_duplicate(
    records: Iterable[Mapping[str, object]],
    video_file: Path,
    account_id: str,
) -> bool:
    """Match a successful/pending history record by account and filename or public URL."""
    for record in records:
        if not _record_targets_account(record, account_id):
            continue
        if not _record_has_active_status(record):
            continue
        if _record_references_video(record, video_file):
            return True
    return False


def remote_posts_have_duplicate(
    posts: Iterable[Mapping[str, object]],
    video_file: Path,
    account_id: str,
) -> bool:
    """Match active Zernio posts conservatively by target Instagram account and media identity."""
    for post in posts:
        if not _post_targets_instagram_account(post, account_id):
            continue
        if not _record_has_active_status(post):
            continue
        if _record_references_video(post, video_file):
            return True
    return False


def _record_targets_account(record: Mapping[str, object], account_id: str) -> bool:
    """Return whether a local record belongs to the selected account."""
    return _normalize_identifier(_string_value(record.get("account_id"))) == _normalize_identifier(
        account_id
    )


def _record_has_active_status(record: Mapping[str, object]) -> bool:
    """Treat draft, queued, and published posts as duplicate-protected states."""
    status = _string_value(record.get("status"))
    return status.casefold() in ACTIVE_POST_STATUSES if status else False


def _record_references_video(record: Mapping[str, object], video_file: Path) -> bool:
    """Check common direct and nested Zernio media fields for a filename match."""
    for value in _candidate_values(record):
        if _value_matches_video(value, video_file):
            return True
    for media_item in _media_items(record):
        for value in _candidate_values(media_item):
            if _value_matches_video(value, video_file):
                return True
    return False


def _candidate_values(record: Mapping[str, object]) -> tuple[object, ...]:
    """Return filename and URL-like fields across current and legacy record shapes."""
    return tuple(
        record.get(field_name)
        for field_name in (
            "filename",
            "filename_stem",
            "video_filename",
            "public_media_url",
            "public_url",
            "url",
            "uploadUrl",
        )
    )


def _media_items(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Read common top-level and platform-specific media item containers."""
    items: list[Mapping[str, object]] = []
    for field_name in ("mediaItems", "media_items", "media", "attachments"):
        raw_items = record.get(field_name)
        if isinstance(raw_items, Mapping):
            items.append(raw_items)
        elif isinstance(raw_items, list):
            items.extend(item for item in raw_items if isinstance(item, Mapping))
    platforms = record.get("platforms")
    platform_entries = [platforms] if isinstance(platforms, Mapping) else platforms
    if isinstance(platform_entries, list):
        for platform in platform_entries:
            if isinstance(platform, Mapping):
                items.extend(_media_items(platform))
    return items


def _post_targets_instagram_account(post: Mapping[str, object], account_id: str) -> bool:
    """Require an Instagram platform and selected account when Zernio supplies both fields."""
    expected_account_id = _normalize_identifier(account_id)
    platform_entries = post.get("platforms")
    if isinstance(platform_entries, Mapping):
        entries = [platform_entries]
    elif isinstance(platform_entries, list):
        entries = [entry for entry in platform_entries if isinstance(entry, Mapping)]
    else:
        entries = []

    matches: list[bool] = []
    for entry in entries:
        platform = _string_value(entry.get("platform")).casefold()
        if platform and platform != "instagram":
            continue
        raw_account = entry.get("accountId") or entry.get("account_id")
        if isinstance(raw_account, Mapping):
            raw_account = raw_account.get("_id") or raw_account.get("id")
        candidate_account_id = _normalize_identifier(_string_value(raw_account))
        if candidate_account_id:
            matches.append(candidate_account_id == expected_account_id)
        elif platform == "instagram":
            matches.append(True)
    if matches:
        return any(matches)

    platform = _string_value(post.get("platform")).casefold()
    if platform and platform != "instagram":
        return False
    top_level_account = post.get("accountId") or post.get("account_id") or post.get("account")
    if isinstance(top_level_account, Mapping):
        top_level_account = top_level_account.get("_id") or top_level_account.get("id")
    candidate_account_id = _normalize_identifier(_string_value(top_level_account))
    return not candidate_account_id or candidate_account_id == expected_account_id


def _value_matches_video(value: object, video_file: Path) -> bool:
    """Match a direct filename, stem, or URL path against the local media file."""
    if not isinstance(value, str) or not value.strip():
        return False
    expected_name = video_file.name.casefold()
    expected_stem = video_file.stem.casefold()
    raw_name = Path(value).name.casefold()
    if raw_name == expected_name or Path(raw_name).stem.casefold() == expected_stem:
        return True
    try:
        url_name = Path(unquote(urlparse(value).path)).name.casefold()
    except ValueError:
        return False
    return bool(url_name) and (
        url_name == expected_name or Path(url_name).stem.casefold() == expected_stem
    )


def _normalize_identifier(value: str) -> str:
    """Normalize opaque account identifiers only for equality comparisons."""
    return value.strip().casefold()


def _string_value(value: object) -> str:
    """Return a string field or a harmless empty value for malformed API data."""
    return value.strip() if isinstance(value, str) else ""
