"""Pending-clip download support built around yt-dlp."""

from .downloader import PendingClipDownloader
from .models import DownloadRequest, DownloadResult, DownloadSummary, MediaInspection
from .yt_dlp_client import (
    UnsupportedMediaError,
    YtDlpClient,
    YtDlpClientError,
    YtDlpDependencyError,
    create_yt_dlp_client,
)

__all__ = [
    "DownloadRequest",
    "DownloadResult",
    "DownloadSummary",
    "MediaInspection",
    "PendingClipDownloader",
    "UnsupportedMediaError",
    "YtDlpClient",
    "YtDlpClientError",
    "YtDlpDependencyError",
    "create_yt_dlp_client",
]
