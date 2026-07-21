"""Non-interactive reuse of the existing Google OAuth token for YouTube uploads."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from collector.models import YoutubeConfig

from .models import YoutubeAuthenticationStatus, YoutubeChannel, YoutubeUploadResult


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_OAUTH_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)


class YoutubeClientError(RuntimeError):
    """Raised when a reusable YouTube client cannot safely perform an API operation."""


class YoutubeDependencyError(YoutubeClientError):
    """Raised when the optional Google API dependencies are not installed yet."""


class YoutubeAuthenticationError(YoutubeClientError):
    """Raised when reusable credentials are unavailable, invalid, or cannot be refreshed."""


class YoutubeClientProtocol(Protocol):
    """The small API surface the uploader needs, kept easy to replace with mocked data."""

    def authentication_status(self, *, include_channel: bool = True) -> YoutubeAuthenticationStatus:
        """Return non-secret reusable-credential and channel status."""

    def upload_short(
        self,
        video_file: Path,
        *,
        title: str,
        description: str,
        tags: tuple[str, ...],
        category_id: str,
        privacy_status: str,
        made_for_kids: bool,
    ) -> YoutubeUploadResult:
        """Upload one normal playable vertical MP4 and return its YouTube identity."""

    def list_uploaded_video_ids(self) -> frozenset[str]:
        """Return recently visible uploads for conservative remote duplicate checks."""


class YoutubeClient:
    """Build a Google API client from configured, external, reusable OAuth files only."""

    def __init__(self, config: YoutubeConfig) -> None:
        self._config = config
        self._service: object | None = None

    def authentication_status(self, *, include_channel: bool = True) -> YoutubeAuthenticationStatus:
        """Inspect configured files and, when possible, verify their current authenticated channel."""
        credentials_available = self._config.oauth_client_credentials_file.is_file()
        token_available = self._config.token_file.is_file()
        if not credentials_available or not token_available:
            missing = []
            if not credentials_available:
                missing.append("OAuth client credentials")
            if not token_available:
                missing.append("reusable token")
            return YoutubeAuthenticationStatus(
                credentials_available=credentials_available,
                token_available=token_available,
                token_reusable=False,
                error=f"Missing {', '.join(missing)}.",
            )
        try:
            self._credentials()
            channel = self._authenticated_channel() if include_channel else None
        except YoutubeClientError as error:
            return YoutubeAuthenticationStatus(
                credentials_available=True,
                token_available=True,
                token_reusable=False,
                error=str(error),
            )
        return YoutubeAuthenticationStatus(
            credentials_available=True,
            token_available=True,
            token_reusable=True,
            channel=channel,
        )

    def upload_short(
        self,
        video_file: Path,
        *,
        title: str,
        description: str,
        tags: tuple[str, ...],
        category_id: str,
        privacy_status: str,
        made_for_kids: bool,
    ) -> YoutubeUploadResult:
        """Perform a resumable upload while preserving the external token file unchanged."""
        if made_for_kids:
            raise YoutubeClientError("This uploader requires selfDeclaredMadeForKids=False.")
        _, MediaFileUpload = _google_dependencies()
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": category_id,
                "tags": list(tags),
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        try:
            request = self._youtube_service().videos().insert(
                part="snippet,status",
                body=body,
                media_body=MediaFileUpload(str(video_file), chunksize=-1, resumable=True),
            )
            response = None
            while response is None:
                _, response = request.next_chunk()
            video_id = response.get("id") if isinstance(response, dict) else None
        except Exception as error:
            raise YoutubeClientError(f"YouTube upload failed: {error}") from error
        if not isinstance(video_id, str) or not video_id.strip():
            raise YoutubeClientError("YouTube upload completed without a video ID.")
        return YoutubeUploadResult(
            video_id=video_id,
            video_url=f"https://www.youtube.com/watch?v={video_id}",
        )

    def list_uploaded_video_ids(self) -> frozenset[str]:
        """List the channel's most recent uploads for a best-effort remote duplicate signal."""
        try:
            channel_response = self._youtube_service().channels().list(
                part="contentDetails", mine=True, maxResults=1
            ).execute()
            items = channel_response.get("items", []) if isinstance(channel_response, dict) else []
            if not items or not isinstance(items[0], dict):
                return frozenset()
            details = items[0].get("contentDetails")
            related = details.get("relatedPlaylists") if isinstance(details, dict) else None
            uploads_id = related.get("uploads") if isinstance(related, dict) else None
            if not isinstance(uploads_id, str) or not uploads_id:
                return frozenset()
            response = self._youtube_service().playlistItems().list(
                part="contentDetails", playlistId=uploads_id, maxResults=50
            ).execute()
        except Exception as error:
            raise YoutubeClientError(f"Could not inspect recent YouTube uploads: {error}") from error
        items = response.get("items", []) if isinstance(response, dict) else []
        return frozenset(
            video_id
            for item in items
            if isinstance(item, dict)
            if isinstance(item.get("contentDetails"), dict)
            if isinstance((video_id := item["contentDetails"].get("videoId")), str) and video_id
        )

    def _authenticated_channel(self) -> YoutubeChannel | None:
        """Read the channel's public name and ID without returning credential values."""
        return load_authenticated_youtube_channel(self._youtube_service())

    def _youtube_service(self) -> object:
        """Build the Google API service lazily so plain project commands require no OAuth package import."""
        if self._service is not None:
            return self._service
        build, _ = _google_dependencies()
        try:
            self._service = build("youtube", "v3", credentials=self._credentials())
        except YoutubeClientError:
            raise
        except Exception as error:
            raise YoutubeClientError(f"Could not build the YouTube API client: {error}") from error
        return self._service

    def _credentials(self) -> object:
        """Load and refresh the configured token in memory without writing during status or upload."""
        if not self._config.oauth_client_credentials_file.is_file():
            raise YoutubeAuthenticationError(
                f"OAuth client credentials are unavailable: {self._config.oauth_client_credentials_file}"
            )
        if not self._config.token_file.is_file():
            raise YoutubeAuthenticationError(
                f"Reusable YouTube token is unavailable: {self._config.token_file}"
            )
        Credentials, Request = _google_auth_dependencies()
        try:
            credentials = Credentials.from_authorized_user_file(
                str(self._config.token_file), list(YOUTUBE_OAUTH_SCOPES)
            )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
        except Exception as error:
            raise YoutubeAuthenticationError(
                "The reusable YouTube token could not be loaded or refreshed. "
                "It was not modified; reconnect only if the source project token is no longer valid."
            ) from error
        if not credentials.valid:
            raise YoutubeAuthenticationError(
                "The reusable YouTube token is not valid. It was not modified; reconnect only if needed."
            )
        return credentials


def create_youtube_client(config: YoutubeConfig) -> YoutubeClient:
    """Create the non-interactive reusable-token client for the configured YouTube channel."""
    return YoutubeClient(config)


def load_authenticated_youtube_channel(service: object) -> YoutubeChannel | None:
    """Read the authenticated channel from a built service for status and first-time login."""
    try:
        response = service.channels().list(part="snippet", mine=True, maxResults=1).execute()
    except Exception as error:
        raise YoutubeClientError(f"Could not read the authenticated YouTube channel: {error}") from error
    items = response.get("items", []) if isinstance(response, dict) else []
    if not items or not isinstance(items[0], dict):
        return None
    channel_id = items[0].get("id")
    snippet = items[0].get("snippet")
    channel_name = snippet.get("title") if isinstance(snippet, dict) else None
    if not isinstance(channel_id, str) or not channel_id:
        return None
    return YoutubeChannel(
        channel_id=channel_id,
        channel_name=channel_name if isinstance(channel_name, str) and channel_name else channel_id,
    )


def _google_dependencies() -> tuple[object, object]:
    """Import Google API dependencies lazily and present one concise install error when absent."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise YoutubeDependencyError(
            "Install google-api-python-client, google-auth, and google-auth-oauthlib for YouTube uploads."
        ) from error
    return build, MediaFileUpload


def _google_auth_dependencies() -> tuple[object, object]:
    """Import Google OAuth classes lazily to keep unrelated pipeline commands lightweight."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as error:
        raise YoutubeDependencyError(
            "Install google-api-python-client, google-auth, and google-auth-oauthlib for YouTube uploads."
        ) from error
    return Credentials, Request
