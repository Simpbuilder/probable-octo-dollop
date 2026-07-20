"""Small HTTP adapter for the Zernio API, isolated from queue orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import ZernioAccount, ZernioPostResult, ZernioPresignedMedia


ZERNIO_API_BASE_URL = "https://zernio.com/api/v1"
VIDEO_CONTENT_TYPE = "video/mp4"


class ZernioClientError(RuntimeError):
    """Base error raised for a recoverable Zernio API or response problem."""


class ZernioCredentialsError(ZernioClientError):
    """Raised when a Zernio API key is unavailable for an explicit command."""


class ZernioDependencyError(ZernioClientError):
    """Raised when the optional HTTP dependency has not been installed."""


class ZernioResponseError(ZernioClientError):
    """Raised when Zernio responds successfully but omits required data."""


class HttpResponseProtocol(Protocol):
    """The narrow response surface used by the client and easy to fake in tests."""

    status_code: int

    def json(self) -> object:
        """Return parsed JSON data."""

    def raise_for_status(self) -> None:
        """Raise an HTTP error for an unsuccessful response."""


class HttpSessionProtocol(Protocol):
    """The small requests-like session surface used by this adapter."""

    def get(self, url: str, **kwargs: object) -> HttpResponseProtocol:
        """Issue an HTTP GET request."""

    def post(self, url: str, **kwargs: object) -> HttpResponseProtocol:
        """Issue an HTTP POST request."""

    def put(self, url: str, **kwargs: object) -> HttpResponseProtocol:
        """Issue an HTTP PUT request."""


class ZernioClientProtocol(Protocol):
    """The Zernio operations consumed by the uploader and mocked in its tests."""

    def list_accounts(self) -> list[ZernioAccount]:
        """Return all connected accounts visible to the configured API key."""

    def list_posts(self, account_id: str) -> list[Mapping[str, object]]:
        """Return recent Instagram posts for remote duplicate checks."""

    def request_presigned_media(self, video_file: Path) -> ZernioPresignedMedia:
        """Request a direct media-upload URL and public media URL."""

    def upload_media(self, video_file: Path, media: ZernioPresignedMedia) -> None:
        """Upload local bytes to the Zernio-provided presigned URL."""

    def create_instagram_reel(
        self,
        *,
        account_id: str,
        public_media_url: str,
        filename: str,
        caption: str,
        publish_now: bool,
    ) -> ZernioPostResult:
        """Create a draft or immediate Instagram Reel post."""


def load_zernio_api_key(
    env_path: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Load ``ZERNIO_API_KEY`` from the root `.env` without exposing its value."""
    env_path = Path(env_path)
    if env_path.exists():
        try:
            from dotenv import load_dotenv
        except ModuleNotFoundError as error:
            raise ZernioDependencyError(
                "python-dotenv is not installed. Run: pip install -r requirements.txt"
            ) from error
        load_dotenv(dotenv_path=env_path, override=False)

    environment = os.environ if environ is None else environ
    api_key = environment.get("ZERNIO_API_KEY", "").strip()
    if not api_key:
        raise ZernioCredentialsError(
            "Missing ZERNIO_API_KEY. Add it to .env using .env.example as a template."
        )
    return api_key


def create_zernio_client(api_key: str) -> "ZernioHttpClient":
    """Create the production HTTP client only for an explicit Zernio command."""
    try:
        import requests
    except ModuleNotFoundError as error:
        raise ZernioDependencyError(
            "requests is not installed. Run: pip install -r requirements.txt"
        ) from error
    return ZernioHttpClient(api_key=api_key, session=requests.Session())


class ZernioHttpClient:
    """HTTP implementation of the documented Zernio account, media, and post APIs."""

    def __init__(self, *, api_key: str, session: HttpSessionProtocol) -> None:
        """Keep the API key private and accept an injectable HTTP session for tests."""
        if not api_key.strip():
            raise ValueError("Zernio API key must not be empty.")
        self._api_key = api_key
        self._session = session

    def list_accounts(self) -> list[ZernioAccount]:
        """List connected accounts while accepting Zernio's documented response wrappers."""
        data = self._request_json("GET", "/accounts", timeout_seconds=30)
        raw_accounts = _extract_list(data, "accounts", "account list")
        accounts: list[ZernioAccount] = []
        for raw_account in raw_accounts:
            account = _parse_account(raw_account)
            if account is not None:
                accounts.append(account)
        return accounts

    def list_posts(self, account_id: str) -> list[Mapping[str, object]]:
        """List recent Instagram posts for the selected account's duplicate check."""
        data = self._request_json(
            "GET",
            "/posts",
            params={"platform": "instagram", "accountId": account_id, "limit": 100},
            timeout_seconds=30,
        )
        return _extract_list(data, "posts", "post list")

    def request_presigned_media(self, video_file: Path) -> ZernioPresignedMedia:
        """Request a documented MP4 presigned upload URL for one completed local Reel."""
        video_file = Path(video_file)
        try:
            file_size = video_file.stat().st_size
        except OSError as error:
            raise ZernioClientError(f"Could not inspect local video file: {error}") from error
        data = self._request_json(
            "POST",
            "/media/presign",
            json_body={
                "filename": video_file.name,
                "contentType": VIDEO_CONTENT_TYPE,
                "size": file_size,
            },
            timeout_seconds=30,
        )
        if not isinstance(data, Mapping):
            raise ZernioResponseError("Zernio returned an invalid media presign response.")
        upload_url = _nonempty_string(data.get("uploadUrl"))
        public_url = _nonempty_string(data.get("publicUrl"))
        if not upload_url or not public_url:
            raise ZernioResponseError("Zernio media presign response is missing uploadUrl or publicUrl.")
        return ZernioPresignedMedia(upload_url=upload_url, public_url=public_url)

    def upload_media(self, video_file: Path, media: ZernioPresignedMedia) -> None:
        """PUT video bytes directly to the presigned URL without bearer credentials."""
        try:
            with Path(video_file).open("rb") as video_handle:
                response = self._session.put(
                    media.upload_url,
                    headers={"Content-Type": VIDEO_CONTENT_TYPE},
                    data=video_handle,
                    timeout=300,
                )
        except OSError as error:
            raise ZernioClientError(f"Could not read local video file: {error}") from error
        except Exception as error:
            raise ZernioClientError("Could not connect to Zernio media storage.") from error
        try:
            response.raise_for_status()
        except Exception as error:
            status_code = getattr(response, "status_code", "unknown")
            raise ZernioClientError(
                f"Zernio media upload failed with HTTP status {status_code}."
            ) from error

    def create_instagram_reel(
        self,
        *,
        account_id: str,
        public_media_url: str,
        filename: str,
        caption: str,
        publish_now: bool,
    ) -> ZernioPostResult:
        """Create a draft by default, or publish immediately only when explicitly requested."""
        payload = {
            "content": caption,
            "publishNow": publish_now,
            "isDraft": not publish_now,
            "mediaItems": [
                {
                    "type": "video",
                    "url": public_media_url,
                    "filename": filename,
                    "mimeType": VIDEO_CONTENT_TYPE,
                }
            ],
            "platforms": [
                {
                    "platform": "instagram",
                    "accountId": account_id,
                    "platformSpecificData": {"contentType": "reels"},
                }
            ],
        }
        data = self._request_json(
            "POST",
            "/posts",
            json_body=payload,
            extra_headers={"x-request-id": str(uuid4())},
            timeout_seconds=30,
        )
        post = _extract_post(data)
        post_id = _nonempty_string(post.get("_id")) or _nonempty_string(post.get("id"))
        if not post_id:
            raise ZernioResponseError("Zernio post response is missing a post ID.")
        status = _nonempty_string(post.get("status")) or ("published" if publish_now else "draft")
        return ZernioPostResult(post_id=post_id, status=status)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout_seconds: int,
    ) -> object:
        """Make a bearer-authenticated API call and normalize transport/JSON failures."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        url = f"{ZERNIO_API_BASE_URL}{path}"
        try:
            if method == "GET":
                response = self._session.get(
                    url, headers=headers, params=dict(params or {}), timeout=timeout_seconds
                )
            else:
                response = self._session.post(
                    url, headers=headers, json=dict(json_body or {}), timeout=timeout_seconds
                )
        except Exception as error:
            raise ZernioClientError(f"Could not connect to Zernio for {path}.") from error
        try:
            response.raise_for_status()
        except Exception as error:
            status_code = getattr(response, "status_code", "unknown")
            raise ZernioClientError(
                f"Zernio {path} request failed with HTTP status {status_code}."
            ) from error
        try:
            return response.json()
        except Exception as error:
            raise ZernioResponseError(f"Zernio {path} response was not valid JSON.") from error


def _extract_list(data: object, field_name: str, description: str) -> list[Mapping[str, object]]:
    """Read a list from top-level or `data` response wrappers."""
    raw_records: object = data
    if isinstance(data, Mapping):
        raw_records = data.get(field_name)
        nested_data = data.get("data")
        if raw_records is None and isinstance(nested_data, Mapping):
            raw_records = nested_data.get(field_name)
        elif raw_records is None and isinstance(nested_data, list):
            raw_records = nested_data
    if not isinstance(raw_records, list):
        raise ZernioResponseError(f"Zernio returned an invalid {description} response.")
    return [record for record in raw_records if isinstance(record, Mapping)]


def _extract_post(data: object) -> Mapping[str, object]:
    """Read a created post from current top-level or nested Zernio response shapes."""
    if not isinstance(data, Mapping):
        raise ZernioResponseError("Zernio returned an invalid post creation response.")
    post = data.get("post")
    if not isinstance(post, Mapping):
        nested_data = data.get("data")
        if isinstance(nested_data, Mapping):
            post = nested_data.get("post")
    if not isinstance(post, Mapping):
        raise ZernioResponseError("Zernio post response did not include post details.")
    return post


def _parse_account(data: Mapping[str, object]) -> ZernioAccount | None:
    """Map a raw account object to safe display fields, ignoring malformed records."""
    account_id = _nonempty_string(data.get("_id")) or _nonempty_string(data.get("id"))
    platform = _nonempty_string(data.get("platform"))
    if not account_id or not platform:
        return None
    profile_id = _nonempty_string(data.get("profileId"))
    profile = data.get("profile")
    if not profile_id and isinstance(profile, Mapping):
        profile_id = _nonempty_string(profile.get("_id")) or _nonempty_string(profile.get("id"))
    inactive_statuses = {"inactive", "disabled", "disconnected", "expired", "error"}
    status = _nonempty_string(data.get("status")).casefold()
    active = status not in inactive_statuses
    for field_name in ("active", "isActive", "connected", "isConnected"):
        if field_name in data and isinstance(data[field_name], bool):
            active = bool(data[field_name])
            break
    return ZernioAccount(
        account_id=account_id,
        platform=platform.casefold(),
        username=_nonempty_string(data.get("username")) or None,
        display_name=(
            _nonempty_string(data.get("displayName"))
            or _nonempty_string(data.get("display_name"))
            or _nonempty_string(data.get("name"))
            or None
        ),
        profile_id=profile_id or None,
        active=active,
    )


def _nonempty_string(value: Any) -> str:
    """Return a stripped non-empty string or an empty value for malformed JSON."""
    return value.strip() if isinstance(value, str) else ""
