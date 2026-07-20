"""Local Streamlit controls for the viral clip pipeline; no pipeline logic lives here."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from cleanup import print_cleanup_plan
from collector import ConfigurationError, load_collector_config
from ui_helpers import (
    UiConfigurationValues,
    append_unique_urls,
    load_dashboard_counts,
    load_failed_items,
    load_ready_videos,
    load_reviewable_clips,
    load_system_availability,
    load_ui_configuration,
    preview_cleanup,
    reject_review_candidates,
    run_confirmed_cleanup,
    run_manual_import,
    run_pipeline_action,
    save_review_custom_hook,
    save_ui_configuration,
    select_review_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    """Render a thin local control surface over the existing pipeline services."""
    st.set_page_config(page_title="Viral Clip Pipeline", layout="wide")
    _initialize_session_state()
    st.title("Viral Clip Pipeline")
    if st.sidebar.button("Refresh", use_container_width=True):
        st.rerun()

    try:
        config = load_collector_config(PROJECT_ROOT / "config")
    except ConfigurationError as error:
        st.error(f"Configuration could not be loaded: {error}")
        return

    dashboard_tab, urls_tab, pipeline_tab, review_tab, videos_tab, logs_tab, config_tab, cleanup_tab = st.tabs(
        [
            "Dashboard",
            "Add URLs",
            "Pipeline",
            "Hook Review",
            "Videos",
            "Logs",
            "Configuration",
            "Cleanup",
        ]
    )
    with dashboard_tab:
        _render_dashboard()
    with urls_tab:
        _render_add_urls()
    with pipeline_tab:
        _render_pipeline_controls()
    with review_tab:
        _render_hook_review(config)
    with videos_tab:
        _render_video_preview(config)
    with logs_tab:
        _render_logs_and_errors(config)
    with config_tab:
        _render_configuration(config)
    with cleanup_tab:
        _render_cleanup_controls()


def _initialize_session_state() -> None:
    """Create simple session-only log and cleanup preview stores."""
    st.session_state.setdefault("pipeline_logs", [])
    st.session_state.setdefault("cleanup_plan", None)


def _render_dashboard() -> None:
    """Show local queue health and dependency availability without displaying credentials."""
    try:
        counts = load_dashboard_counts(PROJECT_ROOT)
        availability = load_system_availability(PROJECT_ROOT)
    except (ConfigurationError, OSError, ValueError) as error:
        st.error(f"Dashboard data is unavailable: {error}")
        return

    metrics = [
        ("URLs waiting", counts.urls_waiting),
        ("Pending metadata", counts.pending_metadata),
        ("Downloaded clips", counts.downloaded_clips),
        ("Awaiting hook generation", counts.awaiting_hook_generation),
        ("Awaiting hook review", counts.awaiting_hook_review),
        ("Ready hooked videos", counts.ready_hooked_videos),
        ("Uploaded or posted", counts.uploaded_or_posted),
        ("Failed items", counts.failed_items),
    ]
    for column, (label, value) in zip(st.columns(4), metrics[:4]):
        column.metric(label, value)
    for column, (label, value) in zip(st.columns(4), metrics[4:]):
        column.metric(label, value)

    st.subheader("Local availability")
    availability_rows = {
        "FFmpeg": availability.ffmpeg,
        "ffprobe": availability.ffprobe,
        "OpenAI API key": availability.openai_api_key,
        "Zernio API key": availability.zernio_api_key,
    }
    st.dataframe(
        [{"Requirement": name, "Available": "Yes" if available else "No"} for name, available in availability_rows.items()],
        hide_index=True,
        use_container_width=True,
    )


def _render_add_urls() -> None:
    """Append validated URL lines without disturbing the current local queue or comments."""
    raw_text = st.text_area("URLs", height=180, placeholder="https://www.reddit.com/r/funny/comments/example/")
    if not st.button("Add URLs", use_container_width=True):
        return
    try:
        result = append_unique_urls(PROJECT_ROOT / "input_urls.txt", raw_text)
    except OSError as error:
        st.error(f"URLs could not be added: {error}")
        return
    st.success(f"Added {result.added} URL(s); skipped {result.duplicates} duplicate(s).")
    if result.invalid_lines:
        st.warning("Invalid lines were not added: " + ", ".join(result.invalid_lines))


def _render_pipeline_controls() -> None:
    """Use existing CLI runner actions for all non-review pipeline stages."""
    buttons = [
        ("Import URLs", _run_manual_import),
        ("Download all", lambda: _run_pipeline(["--download", "--all"])),
        ("Generate hooks for all", lambda: _run_pipeline(["--generate-hooks", "--all"])),
        ("Format all selected hooks", lambda: _run_pipeline(["--format", "--all"])),
        ("Upload one Instagram draft", lambda: _run_pipeline(["--upload-one-instagram"])),
        ("Upload all Instagram drafts", lambda: _run_pipeline(["--upload-instagram", "--all"])),
    ]
    for button_row in (buttons[:3], buttons[3:]):
        for column, (label, action) in zip(st.columns(3), button_row):
            if column.button(label, use_container_width=True):
                action()

    st.divider()
    publish_confirmed = st.checkbox(
        "I confirm this may publish to Instagram immediately.",
        key="publish_now_confirmed",
    )
    publish_one, publish_all = st.columns(2)
    if publish_one.button("Publish one now", disabled=not publish_confirmed, use_container_width=True):
        _run_pipeline(["--upload-one-instagram", "--publish-now"])
    if publish_all.button("Publish all now", disabled=not publish_confirmed, use_container_width=True):
        _run_pipeline(["--upload-instagram", "--all", "--publish-now"])


def _render_hook_review(config) -> None:
    """Review only saved candidates and delegate each metadata write to existing review helpers."""
    try:
        clips = load_reviewable_clips(config)
    except (OSError, ValueError) as error:
        st.error(f"Hook candidates could not be loaded: {error}")
        return
    if not clips:
        st.info("No hook candidates are awaiting review.")
        return
    clip_ids = [clip.unique_id for clip in clips]
    selected_id = st.selectbox("Clip", clip_ids, format_func=lambda clip_id: _clip_label(clips, clip_id))
    clip = next(clip for clip in clips if clip.unique_id == selected_id)
    st.caption(f"Clip ID: {clip.unique_id}")
    st.write(clip.title)
    candidate_columns = st.columns(3)
    for index, candidate in enumerate(clip.hook_candidates):
        with candidate_columns[index]:
            st.write(candidate)
            if st.button(f"Select {index + 1}", key=f"candidate-{clip.unique_id}-{index}"):
                _save_review_action(lambda: select_review_candidate(config, clip.unique_id, index))

    custom_hook = st.text_input("Custom hook", key=f"custom-{clip.unique_id}")
    custom_column, skip_column, reject_column = st.columns(3)
    if custom_column.button("Save custom hook", disabled=not custom_hook.strip(), use_container_width=True):
        _save_review_action(lambda: save_review_custom_hook(config, clip.unique_id, custom_hook))
    if skip_column.button("Skip", use_container_width=True):
        st.info("Skipped without changing stored candidates.")
    if reject_column.button("Reject", use_container_width=True):
        _save_review_action(lambda: reject_review_candidates(config, clip.unique_id))


def _render_video_preview(config) -> None:
    """Play hooked ready videos and show the metadata and upload history associated with each file."""
    try:
        videos = load_ready_videos(config)
    except (OSError, ValueError) as error:
        st.error(f"Ready videos could not be loaded: {error}")
        return
    if not videos:
        st.info("No hooked ready videos are available.")
        return
    for video in videos:
        st.subheader(video.path.name)
        st.video(str(video.path))
        st.caption(
            f"Hook: {video.selected_hook or '(none)'} | "
            f"Processing: {video.processing_status} | Upload: {video.upload_status}"
        )


def _render_logs_and_errors(config) -> None:
    """Show captured runner output alongside retryable errors stored in local metadata."""
    st.subheader("Recent pipeline output")
    logs = st.session_state.pipeline_logs
    if not logs:
        st.info("No UI-run pipeline commands yet.")
    for log in reversed(logs[-10:]):
        st.code(log, language="text")

    st.subheader("Failed items")
    try:
        failed_items = load_failed_items(config)
    except (OSError, ValueError) as error:
        st.error(f"Stored errors could not be loaded: {error}")
        return
    if failed_items:
        st.dataframe(
            [{"Clip ID": item.clip_id, "Title": item.title, "Error": item.error} for item in failed_items],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.success("No stored retryable errors.")


def _render_configuration(config) -> None:
    """Edit a deliberately limited set of settings, validating all JSON before saving any change."""
    try:
        values = load_ui_configuration(config)
    except ValueError as error:
        st.error(f"UI configuration is unavailable: {error}")
        return
    with st.form("configuration-form"):
        downloads_per_run = st.number_input("Maximum downloads per run", min_value=1, value=values.downloads_per_run)
        hook_generations_per_run = st.number_input("Maximum hook generations per run", min_value=1, value=values.hook_generations_per_run)
        formats_per_run = st.number_input("Maximum formats per run", min_value=1, value=values.formats_per_run)
        uploads_per_run = st.number_input("Maximum uploads per run", min_value=1, value=values.uploads_per_run)
        publish_mode = st.selectbox(
            "Instagram upload mode",
            ("draft", "publish_now"),
            index=0 if values.instagram_publish_mode == "draft" else 1,
        )
        caption = st.text_area("Fixed Instagram caption", value=values.instagram_caption)
        account_id = st.text_input("Selected Zernio Instagram account", value=values.instagram_account_id or "")
        automatic_selection = st.checkbox(
            "Enable automatic hook selection", value=values.automatic_hook_selection
        )
        submitted = st.form_submit_button("Save configuration")
    if not submitted:
        return
    try:
        save_ui_configuration(
            PROJECT_ROOT,
            UiConfigurationValues(
                downloads_per_run=int(downloads_per_run),
                hook_generations_per_run=int(hook_generations_per_run),
                formats_per_run=int(formats_per_run),
                uploads_per_run=int(uploads_per_run),
                instagram_publish_mode=publish_mode,
                instagram_caption=caption,
                instagram_account_id=account_id.strip() or None,
                automatic_hook_selection=automatic_selection,
            ),
        )
    except (ConfigurationError, OSError, ValueError) as error:
        st.error(f"Configuration was not saved: {error}")
        return
    st.success("Configuration saved and validated.")


def _render_cleanup_controls() -> None:
    """Preview cleanup plans before confirmed execution, reserving RESET for the destructive reset."""
    safe_column, temporary_column, reset_column = st.columns(3)
    if safe_column.button("Safe cleanup", use_container_width=True):
        _show_cleanup_preview(all_temporary=False, reset_project=False)
    if temporary_column.button("Clear regeneratable files", use_container_width=True):
        _show_cleanup_preview(all_temporary=True, reset_project=False)
    if reset_column.button("Full project reset", use_container_width=True):
        _show_cleanup_preview(all_temporary=False, reset_project=True)

    plan = st.session_state.cleanup_plan
    if plan is None:
        return
    st.subheader("Cleanup preview")
    preview_lines: list[str] = []
    print_cleanup_plan(plan, preview_lines.append)
    st.code("\n".join(preview_lines), language="text")
    if plan.mode == "reset":
        confirmation = st.text_input("Type RESET to confirm full project reset", key="reset-confirmation")
        confirmed = confirmation == "RESET"
    elif plan.mode == "all_temporary":
        confirmed = st.checkbox(
            "I understand this removes regeneratable pending and ready media.",
            key="temporary-cleanup-confirmation",
        )
    else:
        confirmed = st.checkbox("Confirm safe cleanup", key="safe-cleanup-confirmation")
    if st.button("Run displayed cleanup", disabled=not confirmed, use_container_width=True):
        result = run_confirmed_cleanup(plan)
        st.success(
            f"Cleanup complete. Removed {result.removed}, cleared {result.cleared}, "
            f"updated {result.metadata_updated} metadata record(s)."
        )
        if result.errors:
            st.error(f"Cleanup finished with {result.errors} error(s).")
        st.session_state.cleanup_plan = None


def _show_cleanup_preview(*, all_temporary: bool, reset_project: bool) -> None:
    """Build a shared cleanup preview and keep it in session state until explicit confirmation."""
    try:
        st.session_state.cleanup_plan = preview_cleanup(
            PROJECT_ROOT,
            all_temporary=all_temporary,
            reset_project=reset_project,
        )
    except ValueError as error:
        st.error(f"Cleanup preview could not be created: {error}")


def _run_pipeline(arguments: list[str]) -> None:
    """Run a standard existing CLI action and append its captured output to the UI log list."""
    result = run_pipeline_action(arguments)
    _remember_pipeline_result(result.arguments, result.exit_code, result.output)


def _run_manual_import() -> None:
    """Run the existing manual intake helper and append its captured runner output."""
    result = run_manual_import(PROJECT_ROOT)
    _remember_pipeline_result(result.arguments, result.exit_code, result.output)


def _remember_pipeline_result(arguments, exit_code: int, output: str) -> None:
    """Store local terminal-style output in session state and surface success or failure immediately."""
    rendered = f"$ run_pipeline.py {' '.join(arguments)}\n{output or '(no output)'}"
    st.session_state.pipeline_logs.append(rendered)
    if exit_code == 0:
        st.success("Pipeline action completed.")
    else:
        st.warning(f"Pipeline action returned exit code {exit_code}.")


def _save_review_action(action) -> None:
    """Persist one UI review decision without starting generation or formatting."""
    try:
        action()
    except (KeyError, OSError, ValueError) as error:
        st.error(f"Hook review choice was not saved: {error}")
        return
    st.success("Hook review choice saved.")


def _clip_label(clips, clip_id: str) -> str:
    """Render a compact title and ID label for the selected saved candidate set."""
    clip = next(clip for clip in clips if clip.unique_id == clip_id)
    return f"{clip.title} ({clip.unique_id})"


if __name__ == "__main__":
    main()
