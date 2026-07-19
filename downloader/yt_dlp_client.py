"""A narrow, testable yt-dlp adapter for inspecting and downloading one URL."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .models import DownloadRequest, DownloadResult, MediaInspection
from .utils import find_completed_media_file


class YtDlpClientError(RuntimeError):
    """Base error raised when yt-dlp cannot inspect or retrieve media."""


class YtDlpDependencyError(YtDlpClientError):
    """Raised when yt-dlp has not been installed in the active environment."""


class UnsupportedMediaError(YtDlpClientError):
    """Raised for unsupported, unavailable, private, image-only, or deleted media."""


class YtDlpClientProtocol(Protocol):
    """The small yt-dlp surface required by pending-clip orchestration."""

    def inspect(self, request: DownloadRequest) -> MediaInspection:
        """Inspect a URL without writing media files."""

    def download(self, request: DownloadRequest, inspection: MediaInspection) -> DownloadResult:
        """Download one inspected media item to the request output directory."""


YoutubeDlFactory = Callable[[dict[str, Any]], Any]


def create_yt_dlp_client() -> "YtDlpClient":
    """Create a client from the installed yt-dlp package with a useful setup error."""
    try:
        from yt_dlp import YoutubeDL
    except ModuleNotFoundError as error:
        raise YtDlpDependencyError(
            "yt-dlp is not installed. Run: pip install -r requirements.txt"
        ) from error
    return YtDlpClient(YoutubeDL)


class YtDlpClient:
    """Adapter that keeps yt-dlp options and result parsing outside the downloader."""

    def __init__(self, youtube_dl_factory: YoutubeDlFactory) -> None:
        """Accept an injectable ``YoutubeDL`` factory so tests never use the network."""
        self._youtube_dl_factory = youtube_dl_factory

    def inspect(self, request: DownloadRequest) -> MediaInspection:
        """Return video properties and whether yt-dlp selected separate streams."""
        info = self._extract_info(request, download=False)
        if not _has_video_stream(info):
            raise UnsupportedMediaError("No downloadable video was found at this URL.")
        return MediaInspection(
            duration_seconds=_optional_positive_float(info.get("duration")),
            width=_optional_positive_int(info.get("width")),
            height=_optional_positive_int(info.get("height")),
            extension=_optional_string(info.get("ext")),
            requires_ffmpeg=_requires_ffmpeg_merge(info),
        )

    def download(self, request: DownloadRequest, inspection: MediaInspection) -> DownloadResult:
        """Download and locate the final playable media file produced by yt-dlp."""
        info = self._extract_info(request, download=True)
        output_path = self._locate_output_file(request, info)
        return DownloadResult(
            local_file_path=output_path,
            duration_seconds=_optional_positive_float(info.get("duration"))
            or inspection.duration_seconds,
            width=_optional_positive_int(info.get("width")) or inspection.width,
            height=_optional_positive_int(info.get("height")) or inspection.height,
        )

    def _extract_info(self, request: DownloadRequest, *, download: bool) -> Mapping[str, Any]:
        """Run yt-dlp with either inspection or download options and normalize errors."""
        options = _yt_dlp_options(request, download=download)
        try:
            with self._youtube_dl_factory(options) as youtube_dl:
                info = youtube_dl.extract_info(request.source_url, download=download)
        except YtDlpClientError:
            raise
        except Exception as error:
            raise _translate_yt_dlp_error(error) from error

        if not isinstance(info, Mapping):
            raise UnsupportedMediaError("yt-dlp did not return media information for this URL.")
        return info

    def _locate_output_file(self, request: DownloadRequest, info: Mapping[str, Any]) -> Path:
        """Locate the exact final file after yt-dlp optionally merges streams."""
        completed_file = find_completed_media_file(
            request.output_directory,
            request.filename_stem,
            request.preferred_format,
        )
        if completed_file is not None:
            return completed_file

        prepared_path = request.output_template.with_suffix(
            f".{_optional_string(info.get('ext')) or request.preferred_format}"
        )
        if prepared_path.is_file():
            return prepared_path.resolve()
        raise YtDlpClientError("yt-dlp completed without creating a playable media file.")


def _yt_dlp_options(request: DownloadRequest, *, download: bool) -> dict[str, Any]:
    """Build a conservative yt-dlp option set for one extension-preserving file."""
    options: dict[str, Any] = {
        "format": _format_selector(request.preferred_format),
        "outtmpl": str(request.output_template),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": request.retries,
        "fragment_retries": request.retries,
        "socket_timeout": request.timeout_seconds,
        "overwrites": request.overwrite,
        "continuedl": True,
        "nopart": False,
        "skip_download": not download,
    }
    if request.maximum_file_size_bytes is not None:
        options["max_filesize"] = request.maximum_file_size_bytes
    if request.preferred_format.lower() == "mp4":
        options["merge_output_format"] = "mp4"
    return options


def _format_selector(preferred_format: str) -> str:
    """Prefer a playable MP4 while retaining a broadly compatible fallback."""
    if preferred_format.lower() == "mp4":
        return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    return "bv*+ba/b"


def _has_video_stream(info: Mapping[str, Any]) -> bool:
    """Reject image-only and audio-only extractor results before downloading."""
    if _is_video_codec(info.get("vcodec")):
        return True
    formats = info.get("formats")
    if not isinstance(formats, list):
        return False
    return any(
        isinstance(media_format, Mapping) and _is_video_codec(media_format.get("vcodec"))
        for media_format in formats
    )


def _is_video_codec(value: object) -> bool:
    """Return whether yt-dlp identifies a format as containing a video stream."""
    return isinstance(value, str) and value.lower() not in {"", "none"}


def _requires_ffmpeg_merge(info: Mapping[str, Any]) -> bool:
    """Detect yt-dlp's selected multi-format download before invoking it."""
    requested_formats = info.get("requested_formats")
    if isinstance(requested_formats, list) and len(requested_formats) > 1:
        return True
    format_id = info.get("format_id")
    return isinstance(format_id, str) and "+" in format_id


def _translate_yt_dlp_error(error: Exception) -> YtDlpClientError:
    """Classify common extractor failures without depending on yt-dlp internals."""
    message = " ".join(str(error).split()) or "yt-dlp could not download this URL."
    unsupported_markers = (
        "unsupported url",
        "no video formats",
        "no suitable formats",
        "private video",
        "private post",
        "deleted",
        "age-restricted",
        "age restricted",
        "not available",
    )
    if any(marker in message.lower() for marker in unsupported_markers):
        return UnsupportedMediaError(message)
    return YtDlpClientError(message)


def _optional_positive_float(value: object) -> float | None:
    """Normalize an optional positive number from yt-dlp metadata."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _optional_positive_int(value: object) -> int | None:
    """Normalize an optional positive integer from yt-dlp metadata."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _optional_string(value: object) -> str | None:
    """Normalize an optional non-empty string from yt-dlp metadata."""
    if isinstance(value, str) and value.strip():
        return value
    return None
