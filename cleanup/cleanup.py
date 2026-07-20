"""Previewable cleanup routines that never target credentials, config, or upload history."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
import shutil

from collector.file_utils import ensure_path_is_within_directory
from collector.models import ClipMetadata
from collector.storage import load_all_clip_metadata, update_clip_metadata

from .models import CleanupEntry, CleanupMode, CleanupPlan, CleanupResult


MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg"})
TEMPORARY_SUFFIXES = frozenset({".part", ".tmp", ".temp", ".ytdl"})
REGENERATABLE_DIRECTORY_NAMES = ("pending", "plain", "hooked")


class CleanupManager:
    """Build and execute conservative cleanup plans rooted in one project directory."""

    def __init__(self, project_root: Path) -> None:
        """Resolve the project root once so every candidate path can be constrained to it."""
        self._project_root = Path(project_root).resolve()
        self._metadata_file = self._project_root / "metadata" / "clips.json"

    def plan(self, mode: CleanupMode = "safe") -> CleanupPlan:
        """Return a non-destructive preview for the requested supported cleanup mode."""
        if mode not in {"safe", "all_temporary", "reset"}:
            raise ValueError(f"Unsupported cleanup mode: {mode}")
        entries: dict[Path, CleanupEntry] = {}
        self._add_safe_temporary_entries(entries)
        if mode in {"all_temporary", "reset"}:
            self._add_regeneratable_media_entries(entries)
        if mode == "reset":
            self._add_reset_entries(entries)
        return CleanupPlan(
            project_root=self._project_root,
            mode=mode,
            entries=tuple(sorted(entries.values(), key=lambda entry: str(entry.path).casefold())),
        )

    def execute(self, plan: CleanupPlan) -> CleanupResult:
        """Execute a plan created for this root, then repair metadata for removed media paths."""
        if plan.project_root.resolve() != self._project_root:
            raise ValueError("Cleanup plan belongs to a different project root.")
        result = CleanupResult()
        removed_paths: set[Path] = set()
        for entry in plan.entries:
            try:
                self._execute_entry(entry)
            except OSError:
                result.errors += 1
                continue
            if entry.action == "clear_file":
                result.cleared += 1
            else:
                result.removed += 1
                removed_paths.add(entry.path.resolve())
        if plan.mode != "reset" and removed_paths:
            result.metadata_updated = self._reconcile_removed_media(removed_paths)
        return result

    def _add_safe_temporary_entries(self, entries: dict[Path, CleanupEntry]) -> None:
        """Plan only cache, stale atomic-write, partial-download, and empty-output cleanup."""
        for cache_directory in self._iter_cache_directories():
            self._add_entry(entries, cache_directory, "Python cache directory")
        for directory in (
            self._project_root / "metadata",
            self._project_root / "logs",
            self._project_root / "clips",
        ):
            for path in self._iter_files(directory):
                if self._is_temporary_file(path):
                    self._add_entry(entries, path, "temporary pipeline file")
        for directory in self._regeneratable_directories():
            for path in self._iter_files(directory):
                if path.suffix.casefold() in MEDIA_SUFFIXES and self._is_empty_file(path):
                    self._add_entry(entries, path, "empty failed media output")

    def _add_regeneratable_media_entries(self, entries: dict[Path, CleanupEntry]) -> None:
        """Plan only pending and ready files that can be produced from existing metadata."""
        for directory in self._regeneratable_directories():
            for path in self._iter_files(directory):
                if path.suffix.casefold() in MEDIA_SUFFIXES:
                    self._add_entry(entries, path, "regeneratable pipeline media")

    def _add_reset_entries(self, entries: dict[Path, CleanupEntry]) -> None:
        """Add the explicit fresh-batch reset scope without touching config, env, history, or posts."""
        for directory in (
            self._project_root / "clips" / "approved",
            self._project_root / "clips" / "rejected",
            self._project_root / "logs",
        ):
            for path in self._iter_files(directory):
                self._add_entry(entries, path, "project reset file")
        if self._metadata_file.is_file():
            self._add_entry(entries, self._metadata_file, "clip-processing metadata")
        input_file = self._project_root / "input_urls.txt"
        if input_file.exists():
            self._add_entry(
                entries,
                input_file,
                "clear manual URL queue for fresh batch",
                action="clear_file",
            )

    def _add_entry(
        self,
        entries: dict[Path, CleanupEntry],
        path: Path,
        reason: str,
        *,
        action: str = "delete",
    ) -> None:
        """Add one valid candidate once, never deleting Git placeholder files."""
        path = Path(path)
        if path.name == ".gitkeep":
            return
        try:
            validated_path = ensure_path_is_within_directory(path, self._project_root)
        except ValueError:
            return
        entries[validated_path] = CleanupEntry(
            path=validated_path,
            reason=reason,
            action=action,  # type: ignore[arg-type]
        )

    def _iter_files(self, directory: Path) -> Iterable[Path]:
        """Yield normal files under an approved project directory without following escapes."""
        directory = Path(directory)
        if not directory.is_dir():
            return ()
        files: list[Path] = []
        for path in directory.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                files.append(ensure_path_is_within_directory(path, self._project_root))
            except ValueError:
                continue
        return files

    def _iter_cache_directories(self) -> Iterable[Path]:
        """Yield Python cache directories under the project while excluding Git internals."""
        directories: list[Path] = []
        for path in self._project_root.rglob("__pycache__"):
            if not path.is_dir() or ".git" in path.parts:
                continue
            try:
                directories.append(ensure_path_is_within_directory(path, self._project_root))
            except ValueError:
                continue
        return directories

    def _regeneratable_directories(self) -> tuple[Path, Path, Path]:
        """Return the only media directories that broad temporary cleanup may clear."""
        clips_directory = self._project_root / "clips"
        return (
            clips_directory / "pending",
            clips_directory / "ready" / "plain",
            clips_directory / "ready" / "hooked",
        )

    def _is_temporary_file(self, path: Path) -> bool:
        """Recognize only known interrupted-download and atomic-write artifact names."""
        name = path.name.casefold()
        return (
            path.suffix.casefold() in TEMPORARY_SUFFIXES
            or ".part" in name
            or name == "hook-overlay.png"
            or name.endswith(".pyc")
        )

    @staticmethod
    def _is_empty_file(path: Path) -> bool:
        """Treat a zero-byte media artifact as a failed output rather than usable media."""
        try:
            return path.stat().st_size == 0
        except OSError:
            return False

    def _execute_entry(self, entry: CleanupEntry) -> None:
        """Perform one previously previewed operation after validating its path again."""
        path = ensure_path_is_within_directory(entry.path, self._project_root)
        if entry.action == "clear_file":
            path.write_text("", encoding="utf-8", newline="\n")
            return
        if not path.exists():
            return
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _reconcile_removed_media(self, removed_paths: set[Path]) -> int:
        """Return deleted pending/ready records to a truthful retryable metadata state."""
        try:
            clips = load_all_clip_metadata(self._metadata_file)
        except (OSError, ValueError):
            return 0
        updates = 0
        for clip in clips:
            updated_clip = self._reconciled_clip(clip, removed_paths)
            if updated_clip == clip:
                continue
            try:
                update_clip_metadata(self._metadata_file, updated_clip)
            except (OSError, ValueError, KeyError):
                continue
            updates += 1
        return updates

    def _reconciled_clip(
        self,
        clip: ClipMetadata,
        removed_paths: set[Path],
    ) -> ClipMetadata:
        """Clear only local media claims that refer to a planned-and-removed file."""
        local_deleted = self._stored_path_was_removed(clip.local_file_path, removed_paths)
        formatted_deleted = self._stored_path_was_removed(clip.formatted_file_path, removed_paths)
        if not local_deleted and not formatted_deleted:
            return clip

        updated = clip
        if local_deleted:
            updated = replace(
                updated,
                local_file_path=None,
                duration_seconds=None,
                width=None,
                height=None,
                download_status="pending",
                download_error=None,
                processing_status="pending" if updated.processing_status != "posted" else "posted",
            )
        if formatted_deleted:
            updated = replace(
                updated,
                formatted_file_path=None,
                formatted_width=None,
                formatted_height=None,
                format_error=None,
                hook_status=None,
                hook_error=None,
                processing_status="pending" if updated.processing_status == "ready" else updated.processing_status,
            )
        return updated

    def _stored_path_was_removed(
        self,
        stored_path: Path | None,
        removed_paths: set[Path],
    ) -> bool:
        """Resolve modern absolute and legacy project-relative paths for comparison only."""
        if stored_path is None:
            return False
        candidate = Path(stored_path)
        if not candidate.is_absolute():
            candidate = self._project_root / candidate
        return candidate.resolve() in removed_paths


def plan_cleanup(
    project_root: Path,
    *,
    all_temporary: bool = False,
    reset_project: bool = False,
) -> CleanupPlan:
    """Build a safe, broad temporary, or exact reset cleanup preview without deleting anything."""
    if all_temporary and reset_project:
        raise ValueError("all_temporary and reset_project cannot be combined.")
    mode: CleanupMode = "reset" if reset_project else "all_temporary" if all_temporary else "safe"
    return CleanupManager(project_root).plan(mode)


def print_cleanup_plan(plan: CleanupPlan, output_func: Callable[[str], None] = print) -> None:
    """Print every planned operation before a destructive cleanup command can proceed."""
    output_func(f"Cleanup preview: {plan.mode}")
    output_func(f"Project root: {plan.project_root}")
    if not plan.entries:
        output_func("No files will be changed.")
        return
    for entry in plan.entries:
        relative_path = entry.path.relative_to(plan.project_root)
        verb = "CLEAR" if entry.action == "clear_file" else "REMOVE"
        output_func(f"{verb}: {relative_path} ({entry.reason})")


def execute_cleanup_plan(plan: CleanupPlan) -> CleanupResult:
    """Execute a previously previewed cleanup plan through the same reusable manager."""
    return CleanupManager(plan.project_root).execute(plan)


def run_cleanup_command(
    project_root: Path,
    *,
    all_temporary: bool = False,
    reset_project: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> int:
    """Preview and conditionally execute cleanup with the required confirmation strength."""
    try:
        plan = plan_cleanup(
            project_root,
            all_temporary=all_temporary,
            reset_project=reset_project,
        )
    except ValueError as error:
        output_func(f"Cleanup not started: {error}")
        return 2
    print_cleanup_plan(plan, output_func)
    if dry_run:
        output_func("Dry run complete. No files were changed.")
        return 0
    if reset_project:
        confirmation = input_func("Type RESET to clear this project batch: ")
        if confirmation != "RESET":
            output_func("Project reset canceled. Type RESET exactly to continue.")
            return 2
    elif all_temporary and not yes:
        confirmation = input_func("Type YES to remove regeneratable media files: ")
        if confirmation != "YES":
            output_func("Temporary media cleanup canceled.")
            return 2

    result = execute_cleanup_plan(plan)
    output_func("Cleanup complete")
    output_func(f"Removed: {result.removed}")
    output_func(f"Cleared: {result.cleared}")
    output_func(f"Metadata updated: {result.metadata_updated}")
    output_func(f"Errors: {result.errors}")
    return 1 if result.errors else 0
