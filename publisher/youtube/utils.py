"""Pure title, path, and content-hash helpers for YouTube publishing."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from collector.models import ClipMetadata


YOUTUBE_TITLE_MAXIMUM_LENGTH = 100


def build_youtube_title(
    clip: ClipMetadata | None,
    video_file: Path,
    title_template: str,
) -> str:
    """Build a valid YouTube title from saved hook metadata with deterministic fallbacks."""
    source_title = (
        clip.selected_hook
        if clip is not None and clip.selected_hook
        else clip.hook_text
        if clip is not None and clip.hook_text
        else clip.title
        if clip is not None and clip.title
        else video_file.stem
    )
    title = title_template.replace("{title}", source_title)
    normalized = re.sub(r"\s+", " ", title).strip()
    return normalized[:YOUTUBE_TITLE_MAXIMUM_LENGTH].rstrip()


def normalized_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    """Retain configured tags only, without adding any implicit hashtags or AI-generated text."""
    return tuple(tag.strip() for tag in tags if tag.strip())


def sha256_file(path: Path) -> str:
    """Return the stable content hash used for local duplicate protection."""
    digest = sha256()
    with Path(path).open("rb") as media_file:
        while chunk := media_file.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_stored_path(metadata_file: Path, stored_path: Path | None) -> Path | None:
    """Resolve a modern absolute or legacy project-relative metadata path."""
    if stored_path is None:
        return None
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = Path(metadata_file).parent.parent / candidate
    return candidate.resolve()
