"""Thin local UI helpers that present existing pipeline services without reimplementing them."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from collections import Counter
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

from cleanup import CleanupPlan, execute_cleanup_plan, plan_cleanup
from archive import ArchiveManager, ReadyDeletionResult
from collector import ConfigurationError, load_all_clip_metadata, load_collector_config
from collector.manual_url_collector import InvalidManualUrlError, normalize_manual_url
from collector.models import ClipMetadata, CollectorConfig
from hook_generator import (
    reject_hook_candidates,
    save_custom_hook,
    select_hook_candidate,
)
from hook_generator.generator import validate_custom_hook
from publisher import UploadProgressCallback, estimate_batch_duration
from publisher.history import load_post_history
from publisher.youtube import count_pending_youtube_uploads, create_youtube_client
from publisher.youtube.history import load_youtube_history
from ui_models import (
    DashboardCounts,
    ArchiveOverview,
    FailedItem,
    InstagramOverview,
    PipelineActionResult,
    PipelineProgress,
    ReadyVideo,
    SystemAvailability,
    UiConfigurationValues,
    UrlAppendResult,
    YoutubeOverview,
)


def run_pipeline_action(
    arguments: Sequence[str],
    *,
    progress_callback=None,
) -> PipelineActionResult:
    """Run the existing CLI entry point and capture its terminal output for the UI log panel."""
    import run_pipeline

    output = StringIO()
    normalized_arguments = tuple(str(argument) for argument in arguments)
    with redirect_stdout(output), redirect_stderr(output):
        if progress_callback is None:
            exit_code = run_pipeline.main(normalized_arguments)
        else:
            exit_code = run_pipeline.main(
                normalized_arguments, progress_callback=progress_callback
            )
    return PipelineActionResult(
        arguments=normalized_arguments,
        exit_code=exit_code,
        output=output.getvalue().strip(),
    )


def run_manual_import(project_root: Path) -> PipelineActionResult:
    """Invoke the runner's established manual intake service without touching other collectors."""
    import run_pipeline

    project_root = Path(project_root).resolve()
    output = StringIO()
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        return PipelineActionResult(
            arguments=("manual-import",),
            exit_code=2,
            output=f"Manual URL intake not started: {error}",
        )
    with redirect_stdout(output), redirect_stderr(output):
        exit_code = run_pipeline.run_manual_url_collector(
            config, project_root, process_all=True
        )
    return PipelineActionResult(
        arguments=("manual-import", "--all"),
        exit_code=exit_code,
        output=output.getvalue().strip(),
    )


def run_instagram_upload_action(
    project_root: Path,
    *,
    upload_one: bool,
    process_all: bool,
    publish_now: bool,
    post_delay: int | None = None,
    progress_callback: UploadProgressCallback | None = None,
) -> PipelineActionResult:
    """Run the established uploader while allowing the UI to render local batch progress."""
    import run_pipeline

    project_root = Path(project_root).resolve()
    output = StringIO()
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        return PipelineActionResult(
            arguments=("instagram-upload",),
            exit_code=2,
            output=f"Instagram uploader not started: {error}",
        )
    with redirect_stdout(output), redirect_stderr(output):
        exit_code = run_pipeline.run_instagram_uploader(
            config,
            project_root,
            upload_one=upload_one,
            process_all=process_all,
            publish_now=publish_now,
            post_delay=post_delay,
            progress_callback=progress_callback,
        )
    return PipelineActionResult(
        arguments=("instagram-upload", "--all") if process_all else ("instagram-upload",),
        exit_code=exit_code,
        output=output.getvalue().strip(),
    )


def append_unique_urls(input_file: Path, raw_text: str) -> UrlAppendResult:
    """Append only new valid URLs while preserving every existing queue line and comment."""
    input_file = Path(input_file)
    existing_lines = input_file.read_text(encoding="utf-8").splitlines() if input_file.exists() else []
    known_urls: set[str] = set()
    for line in existing_lines:
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        try:
            known_urls.add(normalize_manual_url(stripped_line).normalized_url)
        except InvalidManualUrlError:
            continue

    additions: list[str] = []
    invalid_lines: list[str] = []
    duplicates = 0
    for line in raw_text.splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        try:
            normalized = normalize_manual_url(stripped_line)
        except InvalidManualUrlError:
            invalid_lines.append(stripped_line)
            continue
        if normalized.normalized_url in known_urls:
            duplicates += 1
            continue
        known_urls.add(normalized.normalized_url)
        additions.append(normalized.original_url)

    if additions:
        _write_queue_lines(input_file, [*existing_lines, *additions])
    return UrlAppendResult(
        added=len(additions),
        duplicates=duplicates,
        invalid_lines=tuple(invalid_lines),
    )


def load_dashboard_counts(project_root: Path) -> DashboardCounts:
    """Calculate the dashboard counters from current files and shared metadata storage."""
    project_root = Path(project_root).resolve()
    config = load_collector_config(project_root / "config")
    clips = load_all_clip_metadata(config.metadata_file)
    urls_waiting = _queued_url_count(project_root / "input_urls.txt")
    history = _load_history_safely(config.output_path("metadata") / "zernio_post_history.json")
    ready_directory = config.output_path("ready") / "hooked"
    ready_hooked_videos = len(_direct_media_files(ready_directory))
    return DashboardCounts(
        urls_waiting=urls_waiting,
        pending_metadata=sum(clip.download_status == "pending" for clip in clips),
        downloaded_clips=sum(clip.download_status == "downloaded" for clip in clips),
        awaiting_hook_generation=sum(
            clip.processing_status not in {"rejected", "posted"} and not clip.hook_candidates
            for clip in clips
        ),
        awaiting_hook_review=sum(
            len(clip.hook_candidates) == 3 and clip.selected_hook is None for clip in clips
        ),
        ready_hooked_videos=ready_hooked_videos,
        uploaded_or_posted=len(history),
        pending_youtube_uploads=_youtube_pending_upload_count(config),
        failed_items=len(load_failed_items(config)),
    )


def load_system_availability(project_root: Path) -> SystemAvailability:
    """Check local tools and the presence of environment settings without reading their values out."""
    project_root = Path(project_root).resolve()
    return SystemAvailability(
        ffmpeg=shutil.which("ffmpeg") is not None,
        ffprobe=shutil.which("ffprobe") is not None,
        openai_api_key=_environment_setting_exists(project_root / ".env", "OPENAI_API_KEY"),
        zernio_api_key=_environment_setting_exists(project_root / ".env", "ZERNIO_API_KEY"),
    )


def load_pipeline_progress(
    config: CollectorConfig,
    counts: DashboardCounts | None = None,
) -> PipelineProgress:
    """Summarize remaining work without modifying metadata or invoking a pipeline stage."""
    counts = counts or load_dashboard_counts(config.metadata_file.parent.parent)
    clips = load_all_clip_metadata(config.metadata_file)
    formats_to_run = sum(
        clip.download_status == "downloaded" and clip.processing_status == "pending"
        for clip in clips
    )
    overview = load_instagram_overview(config)
    return PipelineProgress(
        urls_to_import=counts.urls_waiting,
        downloads_to_run=counts.pending_metadata,
        hooks_to_generate=counts.awaiting_hook_generation,
        hooks_to_review=counts.awaiting_hook_review,
        formats_to_run=formats_to_run,
        uploads_to_run=overview.pending_uploads,
        youtube_uploads_to_run=counts.pending_youtube_uploads,
    )


def load_instagram_overview(config: CollectorConfig) -> InstagramOverview:
    """Return local upload state only; never contacts Zernio or exposes credentials."""
    instagram = config.instagram_config
    if instagram is None:
        raise ValueError("Instagram configuration is missing.")
    history = _load_history_safely(config.output_path("metadata") / "zernio_post_history.json")
    history_by_filename = _history_by_filename(history)
    ready_files = _direct_media_files(config.output_path("ready") / "hooked")
    status_counts = Counter(
        str(record.get("status") or "").casefold()
        for record in history
        if isinstance(record, Mapping)
    )
    return InstagramOverview(
        account_username=instagram.account_username,
        publish_mode=instagram.publish_mode,
        fixed_caption=instagram.default_caption,
        pending_uploads=sum(path.name not in history_by_filename for path in ready_files),
        history_total=len(history),
        drafts=status_counts["draft"],
        published=status_counts["published"],
        delay_enabled=instagram.delay_between_posts_enabled,
        delay_seconds=instagram.delay_between_posts_seconds,
        maximum_delay_seconds=instagram.maximum_delay_seconds,
        estimated_batch_seconds=estimate_batch_duration(
            sum(path.name not in history_by_filename for path in ready_files),
            instagram.delay_between_posts_seconds
            if instagram.delay_between_posts_enabled
            else 0,
        ),
    )


def load_youtube_overview(
    config: CollectorConfig,
    *,
    include_channel: bool = False,
) -> YoutubeOverview:
    """Return safe YouTube readiness and local queue history without duplicating uploader logic."""
    youtube = config.youtube_config
    if youtube is None:
        raise ValueError("YouTube configuration is missing.")
    authentication = create_youtube_client(youtube).authentication_status(
        include_channel=include_channel
    )
    history = _load_youtube_history_safely(config.output_path("metadata") / "youtube_upload_history.json")
    return YoutubeOverview(
        credentials_available=authentication.credentials_available,
        token_available=authentication.token_available,
        token_reusable=authentication.token_reusable,
        channel_name=authentication.channel.channel_name if authentication.channel else None,
        channel_id=authentication.channel.channel_id if authentication.channel else None,
        pending_uploads=_youtube_pending_upload_count(config),
        history_total=len(history),
        privacy_status=youtube.privacy_status,
        delay_seconds=youtube.delay_between_uploads_seconds,
        status_detail=authentication.error,
    )


def load_failed_items(config: CollectorConfig) -> list[FailedItem]:
    """Return only stored, retryable errors from current metadata records."""
    failed_items: list[FailedItem] = []
    for clip in load_all_clip_metadata(config.metadata_file):
        error = _clip_error(clip)
        if error is not None:
            failed_items.append(FailedItem(clip.unique_id, clip.title, error))
    return failed_items


def load_reviewable_clips(config: CollectorConfig) -> list[ClipMetadata]:
    """Load only exactly three saved, unselected candidates for manual UI review."""
    return [
        clip
        for clip in load_all_clip_metadata(config.metadata_file)
        if len(clip.hook_candidates) == 3 and clip.selected_hook is None
    ]


def select_review_candidate(config: CollectorConfig, clip_id: str, candidate_index: int) -> None:
    """Delegate exact candidate persistence to the shared review action without generation."""
    clip = _clip_by_id(config, clip_id)
    select_hook_candidate(config.metadata_file, clip, candidate_index)


def save_review_custom_hook(config: CollectorConfig, clip_id: str, custom_text: str) -> None:
    """Validate and save a manual hook through the existing hook-review rules."""
    if config.hook_generation_config is None:
        raise ValueError("Hook generation configuration is missing.")
    clip = _clip_by_id(config, clip_id)
    custom_hook = validate_custom_hook(
        custom_text, config.hook_generation_config.maximum_characters
    )
    save_custom_hook(config.metadata_file, clip, custom_hook)


def reject_review_candidates(config: CollectorConfig, clip_id: str) -> None:
    """Delegate candidate rejection to the shared review action without generation or rendering."""
    reject_hook_candidates(config.metadata_file, _clip_by_id(config, clip_id))


def load_ready_videos(config: CollectorConfig) -> list[ReadyVideo]:
    """Return hooked-ready video cards with stored hook and local upload history status."""
    clips_by_path = {
        _resolved_metadata_path(config, clip.formatted_file_path): clip
        for clip in load_all_clip_metadata(config.metadata_file)
        if clip.formatted_file_path is not None
    }
    history = _load_history_safely(config.output_path("metadata") / "zernio_post_history.json")
    history_by_filename = _history_by_filename(history)
    videos: list[ReadyVideo] = []
    for path in _direct_media_files(config.output_path("ready") / "hooked"):
        clip = clips_by_path.get(path.resolve())
        videos.append(
            ReadyVideo(
                path=path,
                selected_hook=clip.selected_hook if clip is not None else None,
                processing_status=clip.processing_status if clip is not None else "untracked",
                upload_status=history_by_filename.get(path.name, "not uploaded"),
                clip_id=clip.unique_id if clip is not None else None,
                archive_status=clip.archive_status if clip is not None else None,
            )
        )
    return videos


def load_archive_overview(config: CollectorConfig) -> ArchiveOverview:
    """Summarize local archive state only; no formatting, copying, or remote calls occur here."""
    archive_config = config.archive_config
    clips = load_all_clip_metadata(config.metadata_file)
    if archive_config is None:
        return ArchiveOverview(False, 0, 0, 0, None)
    archive_files = _direct_media_files(archive_config.archive_directory)
    return ArchiveOverview(
        enabled=archive_config.enabled,
        archived_videos=sum(clip.archive_status == "archived" for clip in clips),
        missing_archives=sum(
            clip.archive_status == "archived"
            and (clip.archive_path is None or not Path(clip.archive_path).is_file())
            for clip in clips
        ),
        failed_archives=sum(clip.archive_status == "failed" for clip in clips),
        archive_directory=archive_config.archive_directory,
        total_size_bytes=sum(path.stat().st_size for path in archive_files),
    )


def delete_ready_video(config: CollectorConfig, clip_id: str) -> ReadyDeletionResult:
    """Call the guarded deletion service; this never targets source, archive, or upload history files."""
    if config.archive_config is None or config.formatter_config is None:
        return ReadyDeletionResult(clip_id, deleted=False, error="Archive or formatter configuration is missing.")
    return ArchiveManager(
        metadata_file=config.metadata_file,
        ready_directory=config.formatter_config.output_directory,
        config=config.archive_config,
    ).delete_ready_output(clip_id)


def load_ui_configuration(config: CollectorConfig) -> UiConfigurationValues:
    """Extract the limited editable configuration surface for the Streamlit controls."""
    if (
        config.downloader_config is None
        or config.hook_generation_config is None
        or config.formatter_config is None
        or config.instagram_config is None
        or config.youtube_config is None
    ):
        raise ValueError("One or more UI-editable configuration sections are missing.")
    return UiConfigurationValues(
        downloads_per_run=config.downloader_config.downloads_per_run,
        hook_generations_per_run=config.hook_generation_config.maximum_clips_per_run,
        formats_per_run=config.formatter_config.maximum_clips_per_run,
        uploads_per_run=config.instagram_config.maximum_uploads_per_run,
        instagram_publish_mode=config.instagram_config.publish_mode,
        instagram_caption=config.instagram_config.default_caption,
        instagram_account_id=config.instagram_config.account_id,
        automatic_hook_selection=config.hook_generation_config.automatic_selection,
        instagram_delay_enabled=config.instagram_config.delay_between_posts_enabled,
        instagram_delay_seconds=config.instagram_config.delay_between_posts_seconds,
        instagram_maximum_delay_seconds=config.instagram_config.maximum_delay_seconds,
        youtube_enabled=config.youtube_config.enabled,
        youtube_privacy_status=config.youtube_config.privacy_status,
        youtube_delay_seconds=config.youtube_config.delay_between_uploads_seconds,
        youtube_maximum_uploads_per_run=config.youtube_config.maximum_uploads_per_run,
        youtube_default_description=config.youtube_config.default_description,
        youtube_tags=", ".join(config.youtube_config.tags),
        youtube_move_after_upload=config.youtube_config.move_after_upload,
    )


def save_ui_configuration(project_root: Path, values: UiConfigurationValues) -> None:
    """Atomically save validated simple settings while retaining every unrelated config key."""
    _validate_ui_configuration_values(values)
    project_root = Path(project_root).resolve()
    config_directory = project_root / "config"
    collector_data = _load_json_object(config_directory / "collector.json")
    formatter_data = _load_json_object(config_directory / "formatter.json")
    hooks_data = _load_json_object(config_directory / "hooks.json")
    instagram_data = _load_json_object(config_directory / "instagram.json")
    youtube_data = _load_json_object(config_directory / "youtube.json")

    downloader_data = collector_data.get("downloader")
    if not isinstance(downloader_data, dict):
        raise ValueError("collector.json downloader settings are missing.")
    downloader_data["downloads_per_run"] = values.downloads_per_run
    formatter_data["maximum_clips_per_run"] = values.formats_per_run
    hooks_data["maximum_clips_per_run"] = values.hook_generations_per_run
    hooks_data["automatic_selection"] = values.automatic_hook_selection
    instagram_data["maximum_uploads_per_run"] = values.uploads_per_run
    instagram_data["publish_mode"] = values.instagram_publish_mode
    instagram_data["default_caption"] = values.instagram_caption.strip()
    instagram_data["account_id"] = values.instagram_account_id or None
    instagram_data["delay_between_posts_enabled"] = values.instagram_delay_enabled
    instagram_data["delay_between_posts_seconds"] = values.instagram_delay_seconds
    instagram_data["maximum_delay_seconds"] = values.instagram_maximum_delay_seconds
    youtube_data["enabled"] = values.youtube_enabled
    youtube_data["privacy_status"] = values.youtube_privacy_status
    youtube_data["delay_between_uploads_seconds"] = values.youtube_delay_seconds
    youtube_data["maximum_uploads_per_run"] = values.youtube_maximum_uploads_per_run
    youtube_data["default_description"] = values.youtube_default_description
    youtube_data["tags"] = [tag.strip() for tag in values.youtube_tags.split(",") if tag.strip()]
    youtube_data["move_after_upload"] = values.youtube_move_after_upload

    changed_data = {
        "collector.json": collector_data,
        "formatter.json": formatter_data,
        "hooks.json": hooks_data,
        "instagram.json": instagram_data,
        "youtube.json": youtube_data,
    }
    _validate_config_changes(config_directory, changed_data)
    for filename, data in changed_data.items():
        _write_json_object(config_directory / filename, data)


def preview_cleanup(
    project_root: Path,
    *,
    all_temporary: bool = False,
    reset_project: bool = False,
) -> CleanupPlan:
    """Use the shared cleanup planner for UI preview without deleting anything."""
    return plan_cleanup(
        project_root, all_temporary=all_temporary, reset_project=reset_project
    )


def run_confirmed_cleanup(plan: CleanupPlan):
    """Execute a UI-confirmed cleanup plan through the shared cleanup implementation."""
    return execute_cleanup_plan(plan)


def _queued_url_count(input_file: Path) -> int:
    """Count non-comment queue lines using the same shape as manual intake's queue discovery."""
    try:
        lines = input_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(bool(line.strip()) and not line.strip().startswith("#") for line in lines)


def _load_history_safely(history_file: Path) -> list[dict[str, object]]:
    """Let an unreadable optional upload history leave the local dashboard usable."""
    try:
        return load_post_history(history_file)
    except ValueError:
        return []


def _load_youtube_history_safely(history_file: Path) -> list[dict[str, object]]:
    """Keep local dashboard status usable when optional YouTube history is unreadable."""
    try:
        return load_youtube_history(history_file)
    except ValueError:
        return []


def _youtube_pending_upload_count(config: CollectorConfig) -> int:
    """Count locally eligible hooked MP4s without requesting YouTube or touching OAuth state."""
    youtube = config.youtube_config
    if youtube is None:
        return 0
    return count_pending_youtube_uploads(
        history_file=config.output_path("metadata") / "youtube_upload_history.json",
        config=youtube,
    )


def _history_by_filename(records: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Index durable local history records by filename for UI-only status presentation."""
    return {
        str(record.get("filename") or record.get("video_filename") or ""): str(
            record.get("status") or "uploaded"
        )
        for record in records
    }


def _direct_media_files(directory: Path) -> list[Path]:
    """Return direct playable files without recursively scanning other pipeline folders."""
    if not directory.is_dir():
        return []
    return sorted(
        path.resolve()
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in {".mp4", ".mov", ".m4v", ".webm"}
    )


def _clip_error(clip: ClipMetadata) -> str | None:
    """Choose the most specific stored retry error without manufacturing a new status."""
    for error in (
        clip.download_error,
        clip.format_error,
        clip.hook_generation_error,
        clip.hook_error,
        clip.youtube_upload_error,
    ):
        if error:
            return error
    return None


def _clip_by_id(config: CollectorConfig, clip_id: str) -> ClipMetadata:
    """Load one current metadata record rather than relying on potentially stale UI state."""
    for clip in load_all_clip_metadata(config.metadata_file):
        if clip.unique_id == clip_id:
            return clip
    raise KeyError(f"Clip metadata not found: {clip_id}")


def _resolved_metadata_path(config: CollectorConfig, stored_path: Path | None) -> Path:
    """Resolve current and legacy metadata paths only for local preview matching."""
    if stored_path is None:
        return Path()
    path = Path(stored_path)
    return path.resolve() if path.is_absolute() else (config.metadata_file.parent.parent / path).resolve()


def _environment_setting_exists(env_file: Path, key: str) -> bool:
    """Detect a non-empty environment setting without displaying or storing its secret value."""
    if os.environ.get(key, "").strip():
        return True
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    prefix = f"{key}="
    return any(line.strip().startswith(prefix) and bool(line.strip()[len(prefix):].strip()) for line in lines)


def _validate_ui_configuration_values(values: UiConfigurationValues) -> None:
    """Reject invalid simple-control input before changing any checked-in JSON configuration."""
    limits = (
        values.downloads_per_run,
        values.hook_generations_per_run,
        values.formats_per_run,
        values.uploads_per_run,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits):
        raise ValueError("All per-run limits must be positive integers.")
    if values.instagram_publish_mode not in {"draft", "publish_now"}:
        raise ValueError("Instagram publish mode must be draft or publish_now.")
    if not values.instagram_caption.strip():
        raise ValueError("Instagram caption must not be blank.")
    if values.instagram_account_id is not None and not values.instagram_account_id.strip():
        raise ValueError("Instagram account ID must be a non-empty string or blank.")
    if values.instagram_delay_seconds < 0 or values.instagram_maximum_delay_seconds < 0:
        raise ValueError("Instagram post spacing values must be zero or greater.")
    if values.instagram_delay_seconds > values.instagram_maximum_delay_seconds:
        raise ValueError("Instagram post spacing cannot exceed its configured maximum.")
    if values.youtube_privacy_status not in {"public", "private", "unlisted"}:
        raise ValueError("YouTube privacy status must be public, private, or unlisted.")
    if (
        isinstance(values.youtube_delay_seconds, bool)
        or not isinstance(values.youtube_delay_seconds, int)
        or values.youtube_delay_seconds < 0
    ):
        raise ValueError("YouTube upload delay must be a non-negative integer.")
    if (
        isinstance(values.youtube_maximum_uploads_per_run, bool)
        or not isinstance(values.youtube_maximum_uploads_per_run, int)
        or values.youtube_maximum_uploads_per_run <= 0
    ):
        raise ValueError("YouTube maximum uploads per run must be a positive integer.")


def _validate_config_changes(
    config_directory: Path,
    changed_data: Mapping[str, Mapping[str, object]],
) -> None:
    """Validate all cross-file settings in an isolated copy before any real config write occurs."""
    with TemporaryDirectory(prefix="viral-clip-config-") as temporary_directory:
        temporary_config = Path(temporary_directory) / "config"
        shutil.copytree(config_directory, temporary_config)
        for filename, data in changed_data.items():
            _write_json_object(temporary_config / filename, data)
        load_collector_config(temporary_config)


def _load_json_object(path: Path) -> dict[str, Any]:
    """Read a mutable JSON object while keeping malformed settings out of the UI save path."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return data


def _write_json_object(path: Path, data: Mapping[str, object]) -> None:
    """Atomically write one JSON object while preserving valid configuration on interruptions."""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def _write_queue_lines(path: Path, lines: list[str]) -> None:
    """Atomically retain comments and existing URLs while appending validated UI entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if lines:
        content += "\n"
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(content, encoding="utf-8", newline="\n")
    temporary_path.replace(path)
