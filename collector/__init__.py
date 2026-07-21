"""Local, source-agnostic building blocks for collecting video clips."""

from .config import ConfigurationError, load_collector_config
from .collector import CollectionSummary, RedditMetadataCollector
from .manual_url_collector import ManualUrlCollector, ManualUrlSummary
from .models import (
    ArchiveConfig,
    ClipMetadata,
    CollectorConfig,
    DownloaderConfig,
    FormatterConfig,
    HookConfig,
    HookGenerationConfig,
    InstagramConfig,
    PipelineMode,
    SourceConfig,
    YoutubeConfig,
)
from .reddit_client import (
    RedditCredentials,
    RedditCredentialsError,
    create_reddit_client,
    load_reddit_credentials,
)
from .storage import (
    DuplicateClipError,
    clip_exists,
    load_all_clip_metadata,
    load_clip_metadata,
    save_clip_metadata,
    update_clip_metadata,
)

__all__ = [
    "ClipMetadata",
    "ArchiveConfig",
    "CollectionSummary",
    "CollectorConfig",
    "ConfigurationError",
    "DuplicateClipError",
    "DownloaderConfig",
    "FormatterConfig",
    "HookConfig",
    "HookGenerationConfig",
    "InstagramConfig",
    "ManualUrlCollector",
    "ManualUrlSummary",
    "PipelineMode",
    "SourceConfig",
    "clip_exists",
    "create_reddit_client",
    "load_all_clip_metadata",
    "load_clip_metadata",
    "load_collector_config",
    "load_reddit_credentials",
    "RedditCredentials",
    "RedditCredentialsError",
    "RedditMetadataCollector",
    "save_clip_metadata",
    "update_clip_metadata",
    "YoutubeConfig",
]
