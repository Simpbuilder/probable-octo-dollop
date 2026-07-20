"""Compact local Streamlit controls for the viral clip pipeline."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import streamlit as st

from cleanup import print_cleanup_plan
from collector import ConfigurationError, load_collector_config
from collector.models import CollectorConfig
from ui_helpers import (
    UiConfigurationValues,
    append_unique_urls,
    load_dashboard_counts,
    load_failed_items,
    load_instagram_overview,
    load_pipeline_progress,
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

if TYPE_CHECKING:
    from ui_helpers import DashboardCounts, InstagramOverview, PipelineProgress


PROJECT_ROOT = Path(__file__).resolve().parent
PAGES = (
    "Dashboard",
    "Add Clips",
    "Hooks",
    "Ready Videos",
    "Instagram",
    "Cleanup",
    "Settings",
    "Logs",
)


def main() -> None:
    """Render a presentation-only control surface over the existing pipeline services."""
    st.set_page_config(
        page_title="Viral Clip Pipeline",
        page_icon=":material/movie:",
        layout="wide",
    )
    _initialize_session_state()
    try:
        config = load_collector_config(PROJECT_ROOT / "config")
        counts = load_dashboard_counts(PROJECT_ROOT)
        progress = load_pipeline_progress(config, counts)
        instagram = load_instagram_overview(config)
    except (ConfigurationError, OSError, ValueError) as error:
        st.error(f"Local pipeline status is unavailable: {error}", icon=":material/error:")
        return

    page = _render_sidebar(config, counts, instagram)
    _render_header(counts)
    if page == "Dashboard":
        _render_dashboard(counts, progress, instagram)
    elif page == "Add Clips":
        _render_add_clips(progress)
    elif page == "Hooks":
        _render_hook_review(config)
    elif page == "Ready Videos":
        _render_ready_videos(config)
    elif page == "Instagram":
        _render_instagram(config, instagram)
    elif page == "Cleanup":
        _render_cleanup_controls()
    elif page == "Settings":
        _render_settings(config)
    else:
        _render_logs_and_errors(config)


def _initialize_session_state() -> None:
    """Keep navigation, review position, cleanup previews, and local logs stable across reruns."""
    st.session_state.setdefault("navigation", "Dashboard")
    st.session_state.setdefault("pipeline_logs", [])
    st.session_state.setdefault("cleanup_plan", None)
    st.session_state.setdefault("hook_review_index", 0)
    st.session_state.setdefault("last_review_action", None)


def _render_sidebar(
    config: CollectorConfig,
    counts: DashboardCounts,
    instagram: InstagramOverview,
) -> str:
    """Keep global navigation and compact configuration context out of the main workspace."""
    with st.sidebar:
        st.title("Viral clip pipeline")
        st.caption("Local workspace for intake, review, formatting, and explicit publishing.")
        page = st.radio("Navigate", PAGES, key="navigation", label_visibility="collapsed")
        if st.button("Refresh status", icon=":material/refresh:", width="stretch"):
            st.rerun()

        st.subheader("Current status")
        if counts.failed_items:
            st.badge(f"{counts.failed_items} failure(s) need attention", color="red")
        elif counts.urls_waiting or counts.pending_metadata:
            st.badge("Work is waiting", color="orange")
        else:
            st.badge("Queue is clear", color="green")

        with st.expander("Active configuration"):
            st.caption(f"Instagram: @{instagram.account_username or 'not configured'}")
            st.caption(f"Default upload mode: {instagram.publish_mode}")
            st.caption(f"Download limit: {config.downloader_config.downloads_per_run if config.downloader_config else 'n/a'}")
            st.caption(f"Format limit: {config.formatter_config.maximum_clips_per_run if config.formatter_config else 'n/a'}")
    return page


def _render_header(counts: DashboardCounts) -> None:
    """Show the project identity and one plain-language health state at the top of every view."""
    title_column, status_column = st.columns((4, 1), vertical_alignment="center")
    with title_column:
        st.title("Viral clip pipeline", anchor=False)
        st.caption("Collect, review, prepare, and explicitly queue short-form clips.")
    with status_column:
        if counts.failed_items:
            st.badge("Needs attention", icon=":material/error:", color="red")
        elif counts.urls_waiting or counts.pending_metadata:
            st.badge("Pipeline active", icon=":material/pending:", color="orange")
        else:
            st.badge("Ready", icon=":material/check_circle:", color="green")


def _render_dashboard(
    counts: DashboardCounts,
    progress: PipelineProgress,
    instagram: InstagramOverview,
) -> None:
    """Show concise counts, availability, progress, and the next safe pipeline actions."""
    st.subheader("Pipeline overview", anchor=False)
    metrics = (
        ("Queued URLs", counts.urls_waiting),
        ("Pending downloads", counts.pending_metadata),
        ("Downloaded clips", counts.downloaded_clips),
        ("Need hook generation", counts.awaiting_hook_generation),
        ("Awaiting hook selection", counts.awaiting_hook_review),
        ("Ready hooked videos", counts.ready_hooked_videos),
        ("Uploaded videos", counts.uploaded_or_posted),
        ("Failures", counts.failed_items),
    )
    for row in (metrics[:4], metrics[4:]):
        for column, (label, value) in zip(st.columns(4), row):
            column.metric(label, value, border=True)

    left, right = st.columns((3, 2), gap="medium")
    with left:
        _render_pipeline_progress(progress)
    with right:
        _render_dependency_status()

    _render_pipeline_actions(progress, instagram)


def _render_pipeline_progress(progress: PipelineProgress) -> None:
    """Present the remaining queue at each non-destructive stage without starting work."""
    stage_rows = (
        ("URL intake", progress.urls_to_import),
        ("Download", progress.downloads_to_run),
        ("Hook generation", progress.hooks_to_generate),
        ("Hook review", progress.hooks_to_review),
        ("Formatting", progress.formats_to_run),
        ("Upload", progress.uploads_to_run),
    )
    with st.container(border=True):
        st.subheader("Pipeline progress", anchor=False)
        for stage, remaining in stage_rows:
            stage_column, count_column, state_column = st.columns((3, 1, 1), vertical_alignment="center")
            stage_column.write(stage)
            count_column.write(f"{remaining} remaining")
            if remaining:
                state_column.badge("Waiting", color="orange")
            else:
                state_column.badge("Clear", color="green")


def _render_dependency_status() -> None:
    """Display prerequisite availability without revealing secret values."""
    availability = load_system_availability(PROJECT_ROOT)
    with st.container(border=True):
        st.subheader("Local availability", anchor=False)
        requirements = (
            ("FFmpeg", availability.ffmpeg),
            ("ffprobe", availability.ffprobe),
            ("OpenAI", availability.openai_api_key),
            ("Zernio", availability.zernio_api_key),
        )
        for name, available in requirements:
            if available:
                st.success(f"{name} ready", icon=":material/check_circle:")
            else:
                st.warning(f"{name} unavailable", icon=":material/warning:")


def _render_pipeline_actions(progress: PipelineProgress, instagram: InstagramOverview) -> None:
    """Group existing CLI actions by workflow stage and disable empty queues."""
    with st.container(border=True):
        st.subheader("Run pipeline", anchor=False)
        st.caption("Actions reuse the established CLI stages. Nothing publishes without an explicit confirmation.")
        intake, download, hooks = st.columns(3)
        if intake.button(
            "Import queued URLs",
            icon=":material/playlist_add:",
            disabled=not progress.urls_to_import,
            width="stretch",
            type="primary",
        ):
            _run_manual_import()
        if download.button(
            "Download pending clips",
            icon=":material/download:",
            disabled=not progress.downloads_to_run,
            width="stretch",
            type="primary",
        ):
            _run_pipeline(["--download", "--all"])
        if hooks.button(
            "Generate hooks",
            icon=":material/auto_awesome:",
            disabled=not progress.hooks_to_generate,
            width="stretch",
            type="primary",
        ):
            _run_pipeline(["--generate-hooks", "--all"])

        review, format_column, upload = st.columns(3)
        review.button(
            "Review hooks",
            icon=":material/rate_review:",
            disabled=not progress.hooks_to_review,
            width="stretch",
            on_click=_set_navigation,
            args=("Hooks",),
        )
        if format_column.button(
            "Format clips",
            icon=":material/movie:",
            disabled=not progress.formats_to_run,
            width="stretch",
        ):
            _run_pipeline(["--format", "--all"])
        if upload.button(
            "Upload drafts",
            icon=":material/upload:",
            disabled=not instagram.pending_uploads,
            width="stretch",
        ):
            _run_pipeline(["--upload-instagram", "--all"])


def _render_add_clips(progress: PipelineProgress) -> None:
    """Keep manual URL intake focused while preserving the existing file-backed queue behavior."""
    st.subheader("Add clips", anchor=False)
    st.caption("Paste one public URL per line. Existing URLs, comments, and queue history stay intact.")
    with st.container(border=True):
        raw_text = st.text_area(
            "Clip URLs",
            height=190,
            placeholder="https://www.reddit.com/r/funny/comments/example/",
        )
        add_column, import_column, count_column = st.columns((2, 2, 1), vertical_alignment="bottom")
        if add_column.button(
            "Add to queue",
            icon=":material/add_link:",
            type="primary",
            disabled=not raw_text.strip(),
            width="stretch",
        ):
            try:
                result = append_unique_urls(PROJECT_ROOT / "input_urls.txt", raw_text)
            except OSError as error:
                st.error(f"URLs could not be added: {error}", icon=":material/error:")
            else:
                st.success(f"Added {result.added} URL(s); skipped {result.duplicates} duplicate(s).")
                if result.invalid_lines:
                    st.warning("Invalid lines were not added: " + ", ".join(result.invalid_lines))
        if import_column.button(
            "Import queued URLs",
            icon=":material/playlist_add:",
            disabled=not progress.urls_to_import,
            width="stretch",
        ):
            _run_manual_import()
        count_column.metric("Queued", progress.urls_to_import)


def _render_hook_review(config: CollectorConfig) -> None:
    """Review exactly the stored candidates with local navigation and no generation side effects."""
    try:
        clips = load_reviewable_clips(config)
    except (OSError, ValueError) as error:
        st.error(f"Hook candidates could not be loaded: {error}", icon=":material/error:")
        return
    st.subheader("Hook review", anchor=False)
    if message := st.session_state.last_review_action:
        st.success(message, icon=":material/check_circle:")
        st.session_state.last_review_action = None
    if not clips:
        st.info("No hook candidates are awaiting review.", icon=":material/check_circle:")
        return

    current_index = min(st.session_state.hook_review_index, len(clips) - 1)
    st.session_state.hook_review_index = current_index
    clip = clips[current_index]
    previous, position, next_clip = st.columns((1, 2, 1), vertical_alignment="center")
    if previous.button("Previous", icon=":material/arrow_back:", disabled=current_index == 0, width="stretch"):
        st.session_state.hook_review_index -= 1
        st.rerun()
    position.caption(f"{current_index + 1} of {len(clips)} clips awaiting review")
    if next_clip.button("Next", icon=":material/arrow_forward:", disabled=current_index >= len(clips) - 1, width="stretch"):
        st.session_state.hook_review_index += 1
        st.rerun()

    details, preview = st.columns((3, 2), gap="medium")
    with details:
        with st.container(border=True):
            st.subheader(clip.title, anchor=False)
            st.caption(f"Clip ID: `{clip.unique_id}`")
            source_label = clip.source if clip.subreddit is None else f"{clip.source} · r/{clip.subreddit}"
            st.caption(f"Source: {source_label}")
            st.caption(f"Original URL: {clip.source_url}")
    with preview:
        if clip.local_file_path and clip.local_file_path.is_file():
            st.video(str(clip.local_file_path))
        else:
            st.info("Preview becomes available after the clip is downloaded.", icon=":material/video_file:")

    st.caption("Choose one saved candidate. The exact selected text is written to metadata; no hook is generated or rendered here.")
    candidate_columns = st.columns(3)
    for index, candidate in enumerate(clip.hook_candidates):
        with candidate_columns[index]:
            with st.container(border=True):
                st.write(candidate)
                if st.button(
                    "Use this hook",
                    key=f"candidate-{clip.unique_id}-{index}",
                    type="primary" if index == 0 else "secondary",
                    width="stretch",
                ):
                    _save_and_continue(
                        lambda: select_review_candidate(config, clip.unique_id, index),
                        f"Selected: {candidate}",
                        current_index,
                    )

    with st.container(border=True):
        st.subheader("Custom hook", anchor=False)
        custom_hook = st.text_input("Custom hook", key=f"custom-{clip.unique_id}")
        custom, skip, reject = st.columns(3)
        if custom.button(
            "Save and continue",
            icon=":material/save:",
            disabled=not custom_hook.strip(),
            width="stretch",
        ):
            _save_and_continue(
                lambda: save_review_custom_hook(config, clip.unique_id, custom_hook),
                "Custom hook saved.",
                current_index,
            )
        if skip.button("Skip for now", icon=":material/skip_next:", width="stretch"):
            st.session_state.hook_review_index = min(current_index + 1, len(clips) - 1)
            st.rerun()
        if reject.button("Reject candidates", icon=":material/close:", width="stretch"):
            _save_and_continue(
                lambda: reject_review_candidates(config, clip.unique_id),
                "Candidates rejected. The clip remains available for a later explicit regeneration.",
                current_index,
            )


def _render_ready_videos(config: CollectorConfig) -> None:
    """Show hooked-ready videos in a compact grid with the existing next-item upload actions."""
    try:
        videos = load_ready_videos(config)
    except (OSError, ValueError) as error:
        st.error(f"Ready videos could not be loaded: {error}", icon=":material/error:")
        return
    st.subheader("Ready videos", anchor=False)
    if not videos:
        st.info("No hooked ready videos are available.", icon=":material/movie:")
        return

    publish_confirmed = st.checkbox(
        "I confirm this may publish the next eligible video to Instagram immediately.",
        key="ready-videos-publish-confirmed",
    )
    st.caption("Upload actions preserve the CLI's existing next-eligible-file ordering.")
    for start in range(0, len(videos), 2):
        for column, video in zip(st.columns(2), videos[start : start + 2]):
            with column:
                with st.container(border=True):
                    st.video(str(video.path))
                    st.subheader(video.path.name, anchor=False)
                    st.caption(f"Hook: {video.selected_hook or '(none)'}")
                    st.caption(f"Upload status: {video.upload_status}")
                    upload_disabled = video.upload_status != "not uploaded"
                    draft, publish = st.columns(2)
                    if draft.button(
                        "Upload next draft",
                        key=f"draft-{video.path.name}",
                        icon=":material/upload:",
                        disabled=upload_disabled,
                        width="stretch",
                    ):
                        _run_pipeline(["--upload-one-instagram"])
                    if publish.button(
                        "Publish next now",
                        key=f"publish-{video.path.name}",
                        icon=":material/publish:",
                        disabled=upload_disabled or not publish_confirmed,
                        width="stretch",
                    ):
                        _run_pipeline(["--upload-one-instagram", "--publish-now"])
                    st.caption("Folder location")
                    st.code(str(video.path.parent), language="text")


def _render_instagram(config: CollectorConfig, instagram: InstagramOverview) -> None:
    """Present connected-account context and explicit draft-first controls without remote calls."""
    st.subheader("Instagram", anchor=False)
    account, mode, pending, history = st.columns(4)
    account.metric("Connected account", f"@{instagram.account_username or 'not configured'}", border=True)
    mode.metric("Publish mode", instagram.publish_mode, border=True)
    pending.metric("Pending uploads", instagram.pending_uploads, border=True)
    history.metric("Upload history", instagram.history_total, border=True)

    left, right = st.columns((3, 2), gap="medium")
    with left:
        with st.container(border=True):
            st.subheader("Fixed caption", anchor=False)
            values = load_ui_configuration(config)
            with st.form("instagram-caption-form"):
                caption = st.text_area("Caption", value=instagram.fixed_caption, height=150)
                publish_mode = st.selectbox(
                    "Default upload mode",
                    ("draft", "publish_now"),
                    index=0 if values.instagram_publish_mode == "draft" else 1,
                )
                saved = st.form_submit_button("Save Instagram settings", type="primary", width="stretch")
            if saved:
                _save_configuration(replace(values, instagram_caption=caption, instagram_publish_mode=publish_mode))
    with right:
        with st.container(border=True):
            st.subheader("Upload history", anchor=False)
            st.metric("Drafts", instagram.drafts)
            st.metric("Published", instagram.published)
            if st.button(
                "Upload all as drafts",
                icon=":material/upload:",
                type="primary",
                disabled=not instagram.pending_uploads,
                width="stretch",
            ):
                _run_pipeline(["--upload-instagram", "--all"])

    with st.container(border=True):
        st.subheader("Publish immediately", anchor=False)
        st.warning("This bypasses draft review and publishes the next eligible video immediately.", icon=":material/warning:")
        confirmed = st.checkbox(
            "I understand this can publish to Instagram immediately.",
            key="instagram-publish-confirmed",
        )
        publish_one, publish_all = st.columns(2)
        if publish_one.button(
            "Publish next now",
            icon=":material/publish:",
            disabled=not confirmed or not instagram.pending_uploads,
            width="stretch",
        ):
            _run_pipeline(["--upload-one-instagram", "--publish-now"])
        if publish_all.button(
            "Publish all now",
            icon=":material/publish:",
            disabled=not confirmed or not instagram.pending_uploads,
            width="stretch",
        ):
            _run_pipeline(["--upload-instagram", "--all", "--publish-now"])


def _render_cleanup_controls() -> None:
    """Make cleanup scopes legible while delegating preview and confirmation rules to shared code."""
    st.subheader("Cleanup", anchor=False)
    cards = (
        (
            "Safe cleanup",
            "Removes Python caches, partial downloads, temporary metadata files, and zero-byte failed outputs.",
            "Preview safe cleanup",
            False,
            False,
            "secondary",
        ),
        (
            "Clear regeneratable files",
            "Also removes pending downloads and ready renders that can be recreated from metadata.",
            "Preview regeneratable cleanup",
            True,
            False,
            "secondary",
        ),
        (
            "Full project reset",
            "Clears the local batch queue, metadata, pending/ready files, and logs. Protected history and posted videos remain.",
            "Preview full reset",
            False,
            True,
            "primary",
        ),
    )
    for column, (title, description, label, all_temporary, reset_project, button_type) in zip(st.columns(3), cards):
        with column:
            with st.container(border=True):
                st.subheader(title, anchor=False)
                st.caption(description)
                if reset_project:
                    st.error("Requires typing RESET exactly before it can run.", icon=":material/error:")
                elif all_temporary:
                    st.warning("Requires a confirmation checkbox before it can run.", icon=":material/warning:")
                else:
                    st.info("Leaves downloaded clips, ready renders, configuration, history, and posted videos alone.", icon=":material/info:")
                if st.button(label, key=f"cleanup-{title}", type=button_type, width="stretch"):
                    _show_cleanup_preview(all_temporary=all_temporary, reset_project=reset_project)

    plan = st.session_state.cleanup_plan
    if plan is None:
        return
    with st.container(border=True):
        st.subheader("Cleanup preview", anchor=False)
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
        if st.button("Run displayed cleanup", type="primary", disabled=not confirmed, width="stretch"):
            result = run_confirmed_cleanup(plan)
            st.success(
                f"Cleanup complete. Removed {result.removed}, cleared {result.cleared}, "
                f"updated {result.metadata_updated} metadata record(s)."
            )
            if result.errors:
                st.error(f"Cleanup finished with {result.errors} error(s).")
            st.session_state.cleanup_plan = None


def _render_settings(config: CollectorConfig) -> None:
    """Edit the existing limited configuration surface in clear functional groups."""
    st.subheader("Settings", anchor=False)
    try:
        values = load_ui_configuration(config)
    except ValueError as error:
        st.error(f"UI configuration is unavailable: {error}", icon=":material/error:")
        return
    with st.form("settings-form"):
        pipeline, hooks, formatter, instagram = st.columns(4)
        with pipeline:
            st.markdown("**Pipeline limits**")
            downloads = st.number_input("Downloads per run", min_value=1, value=values.downloads_per_run)
        with hooks:
            st.markdown("**Hooks**")
            hook_generations = st.number_input("Generations per run", min_value=1, value=values.hook_generations_per_run)
            automatic_selection = st.checkbox("Automatic selection", value=values.automatic_hook_selection)
        with formatter:
            st.markdown("**Formatter**")
            formats = st.number_input("Formats per run", min_value=1, value=values.formats_per_run)
        with instagram:
            st.markdown("**Instagram**")
            uploads = st.number_input("Uploads per run", min_value=1, value=values.uploads_per_run)
            account_id = st.text_input("Account ID", value=values.instagram_account_id or "")
        caption = st.text_area("Fixed Instagram caption", value=values.instagram_caption)
        submitted = st.form_submit_button("Save validated settings", type="primary", width="stretch")
    if submitted:
        _save_configuration(
            UiConfigurationValues(
                downloads_per_run=int(downloads),
                hook_generations_per_run=int(hook_generations),
                formats_per_run=int(formats),
                uploads_per_run=int(uploads),
                instagram_publish_mode=values.instagram_publish_mode,
                instagram_caption=caption,
                instagram_account_id=account_id.strip() or None,
                automatic_hook_selection=automatic_selection,
            )
        )


def _render_logs_and_errors(config: CollectorConfig) -> None:
    """Keep session-only action output readable while leaving on-disk logs untouched."""
    st.subheader("Logs", anchor=False)
    controls, clear = st.columns((3, 1), vertical_alignment="bottom")
    level_filter = controls.selectbox("Show", ("All", "Info", "Warning", "Error"))
    if clear.button("Clear displayed logs", icon=":material/clear:", width="stretch"):
        st.session_state.pipeline_logs = []
        st.rerun()

    entries = [entry for entry in st.session_state.pipeline_logs if _matches_log_filter(entry, level_filter)]
    with st.container(border=True, height=310):
        if not entries:
            st.caption("No matching UI-run pipeline output.")
        for entry in reversed(entries[-20:]):
            st.code(_log_message(entry), language="text")

    st.subheader("Stored retryable errors", anchor=False)
    try:
        failed_items = load_failed_items(config)
    except (OSError, ValueError) as error:
        st.error(f"Stored errors could not be loaded: {error}", icon=":material/error:")
        return
    if failed_items:
        st.dataframe(
            [{"Clip ID": item.clip_id, "Title": item.title, "Error": item.error} for item in failed_items],
            hide_index=True,
            width="stretch",
        )
    else:
        st.success("No stored retryable errors.", icon=":material/check_circle:")


def _show_cleanup_preview(*, all_temporary: bool, reset_project: bool) -> None:
    """Build a shared cleanup preview and retain it until its existing confirmation succeeds."""
    try:
        st.session_state.cleanup_plan = preview_cleanup(
            PROJECT_ROOT,
            all_temporary=all_temporary,
            reset_project=reset_project,
        )
    except ValueError as error:
        st.error(f"Cleanup preview could not be created: {error}", icon=":material/error:")


def _run_pipeline(arguments: list[str]) -> None:
    """Run the existing CLI entry point and store only its terminal-style result for this UI session."""
    result = run_pipeline_action(arguments)
    _remember_pipeline_result(result.arguments, result.exit_code, result.output)


def _run_manual_import() -> None:
    """Run the established manual intake helper without changing its queue behavior."""
    result = run_manual_import(PROJECT_ROOT)
    _remember_pipeline_result(result.arguments, result.exit_code, result.output)


def _remember_pipeline_result(arguments: tuple[str, ...], exit_code: int, output: str) -> None:
    """Store visible action output in session state without writing or deleting any log files."""
    level = "Info" if exit_code == 0 else "Warning"
    message = f"$ run_pipeline.py {' '.join(arguments)}\n{output or '(no output)'}"
    st.session_state.pipeline_logs.append({"level": level, "message": message})
    if exit_code == 0:
        st.success("Pipeline action completed.", icon=":material/check_circle:")
    else:
        st.warning(f"Pipeline action returned exit code {exit_code}.", icon=":material/warning:")


def _save_and_continue(action: Callable[[], None], message: str, current_index: int) -> None:
    """Persist one hook choice through shared helpers, then move to the next saved candidate set."""
    try:
        action()
    except (KeyError, OSError, ValueError) as error:
        st.error(f"Hook review choice was not saved: {error}", icon=":material/error:")
        return
    st.session_state.last_review_action = message
    st.session_state.hook_review_index = current_index
    st.rerun()


def _save_configuration(values: UiConfigurationValues) -> None:
    """Validate and persist through the existing settings writer, then show an explicit local confirmation."""
    try:
        save_ui_configuration(PROJECT_ROOT, values)
    except (ConfigurationError, OSError, ValueError) as error:
        st.error(f"Configuration was not saved: {error}", icon=":material/error:")
        return
    st.success("Configuration saved and validated.", icon=":material/check_circle:")


def _set_navigation(page: str) -> None:
    """Set sidebar navigation during a widget callback before the radio is instantiated."""
    st.session_state.navigation = page


def _matches_log_filter(entry: object, level_filter: str) -> bool:
    """Support current structured UI logs and harmlessly render sessions created by the older UI."""
    if isinstance(entry, str):
        return level_filter in {"All", "Info"}
    if not isinstance(entry, dict):
        return False
    return level_filter == "All" or entry.get("level") == level_filter


def _log_message(entry: object) -> str:
    """Return current structured output or a legacy string without exposing arbitrary object data."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        message = entry.get("message")
        return message if isinstance(message, str) else "(unreadable UI log entry)"
    return "(unreadable UI log entry)"


if __name__ == "__main__":
    main()
