"""Project-scoped controls for the local Streamlit background pipeline worker."""

from __future__ import annotations

from pathlib import Path

from pipeline_runtime import (
    BackgroundJobManager,
    QueueProgress,
    RuntimeStatus,
    RuntimeStatusStore,
    load_runtime_status,
)


_BACKGROUND_JOB_MANAGERS: dict[Path, BackgroundJobManager] = {}
_JOB_ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "download": ("Download", ("--download", "--all")),
    "generate_hooks": ("Generate hooks", ("--generate-hooks", "--all")),
    "format": ("Format", ("--format", "--all")),
    "upload_drafts": ("Upload Instagram drafts", ("--upload-instagram", "--all")),
    "upload_youtube": ("Upload YouTube Shorts", ("--upload-youtube",)),
    "upload_youtube_one": ("Upload one YouTube Short", ("--upload-youtube-one",)),
    "publish_now": (
        "Publish Instagram",
        ("--upload-instagram", "--publish-now", "--all"),
    ),
}


def runtime_status_file(project_root: Path) -> Path:
    """Return the ignored, local-only runtime status path for one project workspace."""
    return Path(project_root).resolve() / "metadata" / "runtime_status.json"


def start_background_pipeline_job(project_root: Path, job: str) -> RuntimeStatus:
    """Start one full-queue UI action without duplicating CLI stage orchestration."""
    if job not in _JOB_ACTIONS:
        raise ValueError(f"Unknown background pipeline job: {job}")
    resolved_root = Path(project_root).resolve()
    stage, arguments = _JOB_ACTIONS[job]
    manager = _background_job_manager(resolved_root)

    def runner(context) -> int:
        # Import lazily to avoid a circular import with ui_helpers' compatibility exports.
        from ui_helpers import run_pipeline_action

        result = run_pipeline_action(arguments, progress_callback=context.report)
        status = load_runtime_status(runtime_status_file(resolved_root))
        context.report(
            QueueProgress(
                stage=stage,
                current_item=None,
                completed_count=status.completed_count,
                total_count=status.total_count,
                failed_count=status.failed_count,
                remaining_count=0,
                message=result.output.splitlines()[-1] if result.output else "Pipeline action finished.",
            )
        )
        return result.exit_code

    return manager.start(stage, runner)


def request_background_job_stop(project_root: Path) -> RuntimeStatus:
    """Request a graceful stop for the active local batch without touching completed work."""
    return _background_job_manager(Path(project_root).resolve()).request_cancel()


def _background_job_manager(project_root: Path) -> BackgroundJobManager:
    """Reuse one in-process worker manager through Streamlit reruns for this workspace."""
    resolved_root = Path(project_root).resolve()
    manager = _BACKGROUND_JOB_MANAGERS.get(resolved_root)
    if manager is None:
        manager = BackgroundJobManager(RuntimeStatusStore(runtime_status_file(resolved_root)))
        _BACKGROUND_JOB_MANAGERS[resolved_root] = manager
    return manager
