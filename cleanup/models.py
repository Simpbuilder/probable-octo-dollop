"""Typed cleanup plans keep preview and destructive execution separate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CleanupAction = Literal["delete", "clear_file"]
CleanupMode = Literal["safe", "all_temporary", "reset"]


@dataclass(frozen=True, slots=True)
class CleanupEntry:
    """One path-changing operation that has already passed cleanup safety checks."""

    path: Path
    reason: str
    action: CleanupAction = "delete"


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """A deterministic, inspectable set of operations for one cleanup level."""

    project_root: Path
    mode: CleanupMode
    entries: tuple[CleanupEntry, ...]


@dataclass(slots=True)
class CleanupResult:
    """Execution counters reported after a confirmed cleanup plan completes."""

    removed: int = 0
    cleared: int = 0
    metadata_updated: int = 0
    errors: int = 0
