"""Small immutable results returned by archive and ready-output services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """The persisted outcome of copying one hooked ready output into the archive."""

    clip_id: str
    archived: bool
    skipped: bool = False
    archive_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveSummary:
    """Counters for a batch archive repair pass."""

    eligible: int = 0
    archived: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class ArchiveVerification:
    """Read-only archive integrity totals and individual human-readable findings."""

    checked: int = 0
    verified: int = 0
    missing: int = 0
    mismatched: int = 0
    untracked_files: int = 0
    findings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadyDeletionResult:
    """The outcome of deleting one user-selected ready output safely."""

    clip_id: str
    deleted: bool
    path: Path | None = None
    error: str | None = None
