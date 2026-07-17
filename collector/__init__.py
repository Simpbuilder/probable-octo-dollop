"""Local, source-agnostic building blocks for collecting video clips."""

from .config import ConfigurationError, load_collector_config
from .models import ClipMetadata, CollectorConfig, SourceConfig
from .storage import (
    DuplicateClipError,
    clip_exists,
    load_all_clip_metadata,
    load_clip_metadata,
    save_clip_metadata,
)

__all__ = [
    "ClipMetadata",
    "CollectorConfig",
    "ConfigurationError",
    "DuplicateClipError",
    "SourceConfig",
    "clip_exists",
    "load_all_clip_metadata",
    "load_clip_metadata",
    "load_collector_config",
    "save_clip_metadata",
]
