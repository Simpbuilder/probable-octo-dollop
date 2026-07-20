"""Typed values shared by the Zernio client and Instagram upload queue."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZernioAccount:
    """A connected Zernio social account with safe display fields only."""

    account_id: str
    platform: str
    username: str | None = None
    display_name: str | None = None
    profile_id: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class ZernioPresignedMedia:
    """The direct upload and public media locations returned by Zernio."""

    upload_url: str
    public_url: str


@dataclass(frozen=True, slots=True)
class ZernioPostResult:
    """The minimal post result needed for durable duplicate prevention."""

    post_id: str
    status: str
