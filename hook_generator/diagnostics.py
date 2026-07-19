"""Read-only diagnostics for tracing one saved clip's hook-selection path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from collector.models import HookConfig
from collector.storage import load_clip_metadata
from formatter.hooks import HookSelection, resolve_hook_selection


@dataclass(frozen=True, slots=True)
class HookFlowDebug:
    """The saved hook values and resolved formatter choice for one clip."""

    metadata_file: Path
    clip_id: str
    hook_candidates: tuple[str, ...]
    selected_hook: str | None
    selection: HookSelection | None


def inspect_hook_flow(
    metadata_file: Path,
    clip_id: str,
    hook_config: HookConfig,
    *,
    manual_hook: str | None = None,
) -> HookFlowDebug:
    """Load exactly one clip from the configured JSON store without generating or rendering."""
    resolved_metadata_file = Path(metadata_file).resolve()
    clip = load_clip_metadata(resolved_metadata_file, clip_id)
    if clip is None:
        raise KeyError(f"Clip ID was not found in metadata file: {clip_id}")
    return HookFlowDebug(
        metadata_file=resolved_metadata_file,
        clip_id=clip.unique_id,
        hook_candidates=clip.hook_candidates,
        selected_hook=clip.selected_hook,
        selection=resolve_hook_selection(clip, hook_config, manual_hook),
    )
