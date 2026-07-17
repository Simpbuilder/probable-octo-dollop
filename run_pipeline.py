"""Demonstrate the local collector architecture without contacting any source."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from collector import (
    ClipMetadata,
    clip_exists,
    load_clip_metadata,
    load_collector_config,
    save_clip_metadata,
)


def main() -> None:
    """Load config, round-trip example metadata, and verify duplicate detection."""
    project_root = Path(__file__).resolve().parent
    config = load_collector_config(project_root / "config")

    example_clip = ClipMetadata(
        unique_id="reddit-demo-post-001",
        source="reddit",
        subreddit="funny",
        source_post_id="demo-post-001",
        source_url="https://www.reddit.com/r/funny/comments/demo-post-001",
        title="Example collector metadata record",
        author="example_creator",
        score=1250,
        comment_count=84,
        created_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
        media_url="https://example.invalid/video.mp4",
        local_file_path=config.output_path("pending") / "reddit-demo-post-001.mp4",
    )

    with TemporaryDirectory(prefix="viral-clip-pipeline-") as temporary_directory:
        metadata_file = Path(temporary_directory) / config.metadata_file.name
        save_clip_metadata(metadata_file, example_clip)
        loaded_clip = load_clip_metadata(metadata_file, example_clip.unique_id)

        print(f"Loaded metadata: {loaded_clip.to_dict() if loaded_clip else None}")
        print(f"Duplicate detected: {clip_exists(metadata_file, example_clip)}")


if __name__ == "__main__":
    main()
