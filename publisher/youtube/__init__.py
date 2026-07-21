"""YouTube Shorts publishing services with reusable Google OAuth token support."""

from .client import (
    YoutubeAuthenticationError,
    YoutubeClient,
    YoutubeClientError,
    YoutubeDependencyError,
    create_youtube_client,
)
from .models import (
    YoutubeAuthenticationStatus,
    YoutubeChannel,
    YoutubeUploadProgress,
    YoutubeUploadResult,
    YoutubeUploadSummary,
)
from .uploader import YoutubeUploader, YoutubeUploadProgressCallback, count_pending_youtube_uploads

__all__ = [
    "YoutubeAuthenticationError",
    "YoutubeAuthenticationStatus",
    "YoutubeChannel",
    "YoutubeClient",
    "YoutubeClientError",
    "YoutubeDependencyError",
    "YoutubeUploadProgress",
    "YoutubeUploadProgressCallback",
    "YoutubeUploadResult",
    "YoutubeUploadSummary",
    "YoutubeUploader",
    "create_youtube_client",
    "count_pending_youtube_uploads",
]
