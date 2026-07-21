"""Explicit first-time browser OAuth setup for this project's YouTube account."""

from __future__ import annotations

from pathlib import Path

from .client import (
    YOUTUBE_OAUTH_SCOPES,
    YoutubeAuthenticationError,
    YoutubeDependencyError,
    load_authenticated_youtube_channel,
)
from .models import YoutubeChannel


def login_to_youtube(client_secret_file: Path, token_file: Path) -> YoutubeChannel:
    """Open Google's consent flow, save the resulting token, and return the channel identity."""
    client_secret_path = Path(client_secret_file).resolve()
    token_path = Path(token_file).resolve()
    if not client_secret_path.is_file():
        raise YoutubeAuthenticationError(
            f"YouTube OAuth client file not found: {client_secret_path}. "
            "Download it from Google Cloud and save it as client_secret.json in the project root."
        )

    InstalledAppFlow, build = _oauth_dependencies()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path),
            scopes=list(YOUTUBE_OAUTH_SCOPES),
        )
        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )
    except Exception as error:
        raise YoutubeAuthenticationError(f"YouTube login did not complete: {error}") from error

    try:
        token_json = credentials.to_json()
        if not isinstance(token_json, str) or not token_json.strip():
            raise ValueError("Google returned an empty token.")
        _write_token_atomically(token_path, token_json)
    except Exception as error:
        raise YoutubeAuthenticationError(f"Could not save the YouTube token: {error}") from error

    try:
        service = build("youtube", "v3", credentials=credentials)
        channel = load_authenticated_youtube_channel(service)
    except YoutubeAuthenticationError:
        raise
    except Exception as error:
        raise YoutubeAuthenticationError(
            f"YouTube login succeeded, but the channel could not be read: {error}"
        ) from error
    if channel is None:
        raise YoutubeAuthenticationError(
            "YouTube login succeeded, but no channel was found for the authenticated account."
        )
    return channel


def _write_token_atomically(token_file: Path, token_json: str) -> None:
    """Write sensitive token data without leaving a partially written final file."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = token_file.with_suffix(f"{token_file.suffix}.tmp")
    temporary_file.write_text(token_json, encoding="utf-8")
    temporary_file.replace(token_file)


def _oauth_dependencies() -> tuple[object, object]:
    """Load browser OAuth dependencies only when the explicit login command is used."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise YoutubeDependencyError(
            "Install google-api-python-client, google-auth, and google-auth-oauthlib for YouTube login."
        ) from error
    return InstalledAppFlow, build
