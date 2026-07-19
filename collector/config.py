"""JSON configuration loading and validation for the local collector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CollectorConfig,
    DownloaderConfig,
    FormatterConfig,
    HookConfig,
    HookGenerationConfig,
    PipelineMode,
    SourceConfig,
)


REQUIRED_OUTPUT_FOLDERS = frozenset({"pending", "approved", "rejected", "ready", "posted", "metadata"})
REDDIT_SORTING_MODES = frozenset({"hot", "new", "top"})
REDDIT_TOP_TIME_FILTERS = frozenset({"day", "week", "month", "year", "all"})
PIPELINE_MODES = frozenset({"reddit_api", "manual_urls", "both"})


class ConfigurationError(ValueError):
    """Raised when a collector configuration file is missing or invalid."""


def load_collector_config(config_directory: Path) -> CollectorConfig:
    """Load, validate, and resolve the JSON configuration in ``config_directory``.

    Relative output paths are resolved from the project root, which is the
    parent directory of ``config_directory``.
    """
    config_directory = Path(config_directory)
    project_root = config_directory.parent
    sources_data = _load_json_object(config_directory / "sources.json")
    collector_data = _load_json_object(config_directory / "collector.json")

    source_configs = _parse_source_configs(sources_data)
    output_folders = _parse_output_folders(collector_data, project_root)
    metadata_file = _resolve_path(
        _required_string(collector_data, "metadata_file", "collector.json"), project_root
    )
    pipeline_mode = _optional_string(
        collector_data, "pipeline_mode", "collector.json", default="reddit_api"
    )
    downloader_config = _parse_downloader_config(collector_data, project_root)
    formatter_config = _parse_formatter_config(
        _load_optional_json_object(config_directory / "formatter.json"), project_root
    )
    hook_generation_config = _parse_hook_generation_config(
        _load_optional_json_object(config_directory / "hooks.json")
    )

    config = CollectorConfig(
        source_configs=source_configs,
        output_folders=output_folders,
        metadata_file=metadata_file,
        pipeline_mode=pipeline_mode,  # type: ignore[arg-type]
        downloader_config=downloader_config,
        formatter_config=formatter_config,
        hook_generation_config=hook_generation_config,
    )
    _validate_collector_config(config)
    return config


def _load_json_object(path: Path) -> Mapping[str, Any]:
    """Read a JSON object from a configuration file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"Invalid JSON in {path}: {error.msg}") from error

    if not isinstance(data, dict):
        raise ConfigurationError(f"Configuration file must contain a JSON object: {path}")
    return data


def _load_optional_json_object(path: Path) -> Mapping[str, Any] | None:
    """Load an optional JSON object so older project configurations remain valid."""
    if not path.exists():
        return None
    return _load_json_object(path)


def _parse_source_configs(data: Mapping[str, Any]) -> dict[str, SourceConfig]:
    """Convert the source settings JSON object into typed source settings."""
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ConfigurationError("sources.json must contain a non-empty 'sources' object.")

    source_configs: dict[str, SourceConfig] = {}
    for name, raw_config in raw_sources.items():
        if not isinstance(name, str) or not isinstance(raw_config, dict):
            raise ConfigurationError("Each source must have a string name and object settings.")
        try:
            source_configs[name] = SourceConfig(
                name=name,
                enabled=_required_bool(raw_config, "enabled", f"source '{name}'"),
                subreddits=_optional_string_tuple(raw_config, "subreddits", f"source '{name}'"),
                minimum_score=_required_int(raw_config, "minimum_score", f"source '{name}'"),
                maximum_clip_length_seconds=_required_int(
                    raw_config, "maximum_clip_length_seconds", f"source '{name}'"
                ),
                maximum_post_age_days=_required_int(
                    raw_config, "maximum_post_age_days", f"source '{name}'"
                ),
                sorting_mode=_required_string(raw_config, "sorting_mode", f"source '{name}'"),
                top_time_filter=_optional_string(
                    raw_config, "top_time_filter", f"source '{name}'", default="week"
                ),
                posts_to_inspect=_required_int(raw_config, "posts_to_inspect", f"source '{name}'"),
                allow_nsfw=_optional_bool(raw_config, "allow_nsfw", f"source '{name}'", default=False),
            )
        except ValueError as error:
            raise ConfigurationError(f"Invalid settings for source '{name}': {error}") from error
    return source_configs


def _parse_output_folders(data: Mapping[str, Any], project_root: Path) -> dict[str, Path]:
    """Resolve configured output directories from the collector settings."""
    raw_folders = data.get("output_folders")
    if not isinstance(raw_folders, dict):
        raise ConfigurationError("collector.json must contain an 'output_folders' object.")

    missing_folders = REQUIRED_OUTPUT_FOLDERS.difference(raw_folders)
    if missing_folders:
        missing_names = ", ".join(sorted(missing_folders))
        raise ConfigurationError(f"collector.json is missing output folders: {missing_names}")

    output_folders: dict[str, Path] = {}
    for name, raw_path in raw_folders.items():
        if not isinstance(name, str) or not isinstance(raw_path, str) or not raw_path.strip():
            raise ConfigurationError("Output folder names and paths must be non-empty strings.")
        output_folders[name] = _resolve_path(raw_path, project_root)
    return output_folders


def _parse_downloader_config(data: Mapping[str, Any], project_root: Path) -> DownloaderConfig:
    """Load downloader settings while retaining safe defaults for older configs."""
    raw_config = data.get("downloader", {})
    if not isinstance(raw_config, dict):
        raise ConfigurationError("collector.json field 'downloader' must be an object.")

    try:
        return DownloaderConfig(
            directory=_resolve_path(
                _optional_string(
                    raw_config, "directory", "downloader", default="clips/pending"
                ),
                project_root,
            ),
            preferred_format=_optional_string(
                raw_config, "preferred_format", "downloader", default="mp4"
            ).lower(),
            maximum_duration_seconds=_optional_positive_int_or_none(
                raw_config, "maximum_duration_seconds", "downloader", default=90
            ),
            maximum_file_size_bytes=_optional_positive_int_or_none(
                raw_config, "maximum_file_size_bytes", "downloader", default=104_857_600
            ),
            retries=_optional_nonnegative_int(raw_config, "retries", "downloader", default=2),
            timeout_seconds=_optional_positive_int(
                raw_config, "timeout_seconds", "downloader", default=30
            ),
            overwrite=_optional_bool(raw_config, "overwrite", "downloader", default=False),
            downloads_per_run=_optional_positive_int(
                raw_config, "downloads_per_run", "downloader", default=5
            ),
            enabled=_optional_bool(raw_config, "enabled", "downloader", default=False),
        )
    except ValueError as error:
        raise ConfigurationError(f"Invalid downloader settings: {error}") from error


def _parse_formatter_config(
    data: Mapping[str, Any] | None,
    project_root: Path,
) -> FormatterConfig | None:
    """Load optional vertical formatter settings from ``config/formatter.json``."""
    if data is None:
        return None

    try:
        return FormatterConfig(
            output_directory=_resolve_path(
                _required_string(data, "output_directory", "formatter.json"), project_root
            ),
            enabled=_required_bool(data, "enabled", "formatter.json"),
            output_width=_required_int(data, "output_width", "formatter.json"),
            output_height=_required_int(data, "output_height", "formatter.json"),
            background_color=_required_string(data, "background_color", "formatter.json"),
            horizontal_margin=_required_int(data, "horizontal_margin", "formatter.json"),
            top_text_area_height=_required_int(data, "top_text_area_height", "formatter.json"),
            bottom_margin=_required_int(data, "bottom_margin", "formatter.json"),
            maximum_video_width=_required_int(data, "maximum_video_width", "formatter.json"),
            maximum_video_height=_required_int(data, "maximum_video_height", "formatter.json"),
            crop_mode=_required_string(data, "crop_mode", "formatter.json"),  # type: ignore[arg-type]
            output_frame_rate=_required_int(data, "output_frame_rate", "formatter.json"),
            video_codec=_required_string(data, "video_codec", "formatter.json"),
            audio_codec=_required_string(data, "audio_codec", "formatter.json"),
            crf=_required_int(data, "crf", "formatter.json"),
            encoding_preset=_required_string(data, "encoding_preset", "formatter.json"),
            overwrite=_required_bool(data, "overwrite", "formatter.json"),
            maximum_clips_per_run=_required_int(
                data, "maximum_clips_per_run", "formatter.json"
            ),
            hook=_parse_hook_config(data, project_root),
        )
    except ValueError as error:
        raise ConfigurationError(f"Invalid formatter settings: {error}") from error


def _parse_hook_config(data: Mapping[str, Any], project_root: Path) -> HookConfig:
    """Load optional hook overlay settings while preserving older formatter files."""
    raw_config = data.get("hook")
    if raw_config is None:
        return HookConfig()
    if not isinstance(raw_config, dict):
        raise ConfigurationError("formatter.json field 'hook' must be an object.")

    try:
        return HookConfig(
            enabled=_required_bool(raw_config, "enabled", "formatter.json hook"),
            font_path=_optional_path_or_none(raw_config, "font_path", "formatter.json hook", project_root),
            font_size=_required_int(raw_config, "font_size", "formatter.json hook"),
            font_color=_required_string(raw_config, "font_color", "formatter.json hook"),
            maximum_text_width=_required_int(
                raw_config, "maximum_text_width", "formatter.json hook"
            ),
            maximum_lines=_required_int(raw_config, "maximum_lines", "formatter.json hook"),
            line_spacing=_required_int(raw_config, "line_spacing", "formatter.json hook"),
            horizontal_alignment=_required_string(
                raw_config, "horizontal_alignment", "formatter.json hook"
            ),  # type: ignore[arg-type]
            vertical_position=_required_int(raw_config, "vertical_position", "formatter.json hook"),
            text_box_height=_required_int(raw_config, "text_box_height", "formatter.json hook"),
            text_padding=_required_int(raw_config, "text_padding", "formatter.json hook"),
            fallback_to_source_title=_required_bool(
                raw_config, "fallback_to_source_title", "formatter.json hook"
            ),
            minimum_font_size=_required_int(
                raw_config, "minimum_font_size", "formatter.json hook"
            ),
            automatic_font_shrinking=_required_bool(
                raw_config, "automatic_font_shrinking", "formatter.json hook"
            ),
            outline_color=_optional_string_or_none(
                raw_config, "outline_color", "formatter.json hook"
            ),
            outline_width=_required_int(raw_config, "outline_width", "formatter.json hook"),
            shadow_color=_optional_string_or_none(
                raw_config, "shadow_color", "formatter.json hook"
            ),
            shadow_offset=_required_int(raw_config, "shadow_offset", "formatter.json hook"),
        )
    except ValueError as error:
        raise ConfigurationError(f"Invalid formatter hook settings: {error}") from error


def _parse_hook_generation_config(data: Mapping[str, Any] | None) -> HookGenerationConfig:
    """Load optional OpenAI hook-generation settings with safe disabled defaults."""
    if data is None:
        return HookGenerationConfig()
    try:
        return HookGenerationConfig(
            enabled=_required_bool(data, "enabled", "hooks.json"),
            model=_required_string(data, "model", "hooks.json"),
            maximum_characters=_required_int(data, "maximum_characters", "hooks.json"),
            maximum_clips_per_run=_required_int(data, "maximum_clips_per_run", "hooks.json"),
            automatic_selection=_required_bool(data, "automatic_selection", "hooks.json"),
        )
    except ValueError as error:
        raise ConfigurationError(f"Invalid hook generation settings: {error}") from error


def _validate_collector_config(config: CollectorConfig) -> None:
    """Apply cross-file validation after both configuration files are loaded."""
    if not config.enabled_sources:
        raise ConfigurationError("At least one source must be enabled.")
    if config.pipeline_mode not in PIPELINE_MODES:
        allowed_modes = ", ".join(sorted(PIPELINE_MODES))
        raise ConfigurationError(f"pipeline_mode must be one of: {allowed_modes}.")
    reddit_config = config.source_configs.get("reddit")
    if reddit_config and reddit_config.enabled and not reddit_config.subreddits:
        raise ConfigurationError("An enabled Reddit source requires at least one subreddit.")
    if reddit_config and reddit_config.sorting_mode not in REDDIT_SORTING_MODES:
        allowed_modes = ", ".join(sorted(REDDIT_SORTING_MODES))
        raise ConfigurationError(f"Reddit sorting_mode must be one of: {allowed_modes}.")
    if reddit_config and reddit_config.top_time_filter not in REDDIT_TOP_TIME_FILTERS:
        allowed_filters = ", ".join(sorted(REDDIT_TOP_TIME_FILTERS))
        raise ConfigurationError(f"Reddit top_time_filter must be one of: {allowed_filters}.")
    if config.metadata_file.parent != config.output_path("metadata"):
        raise ConfigurationError("metadata_file must be located inside the metadata output folder.")
    if (
        config.downloader_config is not None
        and config.downloader_config.directory != config.output_path("pending")
    ):
        raise ConfigurationError("downloader.directory must match the pending output folder.")
    if (
        config.formatter_config is not None
        and config.formatter_config.output_directory != config.output_path("ready")
    ):
        raise ConfigurationError("formatter.output_directory must match the ready output folder.")


def _resolve_path(raw_path: str, project_root: Path) -> Path:
    """Resolve a config path relative to the project root when needed."""
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def _required_string(data: Mapping[str, Any], field_name: str, context: str) -> str:
    """Read a required string field from a JSON object."""
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} field '{field_name}' must be a non-empty string.")
    return value


def _required_bool(data: Mapping[str, Any], field_name: str, context: str) -> bool:
    """Read a required boolean field from a JSON object."""
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{context} field '{field_name}' must be a boolean.")
    return value


def _required_int(data: Mapping[str, Any], field_name: str, context: str) -> int:
    """Read a required integer field from a JSON object."""
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{context} field '{field_name}' must be an integer.")
    return value


def _optional_string(
    data: Mapping[str, Any], field_name: str, context: str, *, default: str
) -> str:
    """Read an optional non-empty string field from a JSON object."""
    value = data.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} field '{field_name}' must be a non-empty string.")
    return value


def _optional_string_or_none(
    data: Mapping[str, Any],
    field_name: str,
    context: str,
) -> str | None:
    """Read a nullable optional string field from a JSON object."""
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} field '{field_name}' must be a non-empty string or null.")
    return value


def _optional_path_or_none(
    data: Mapping[str, Any],
    field_name: str,
    context: str,
    project_root: Path,
) -> Path | None:
    """Read a nullable local path and resolve relative values from the project root."""
    value = _optional_string_or_none(data, field_name, context)
    return _resolve_path(value, project_root) if value is not None else None


def _optional_bool(
    data: Mapping[str, Any], field_name: str, context: str, *, default: bool
) -> bool:
    """Read an optional boolean field from a JSON object."""
    value = data.get(field_name, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{context} field '{field_name}' must be a boolean.")
    return value


def _optional_positive_int_or_none(
    data: Mapping[str, Any], field_name: str, context: str, *, default: int | None
) -> int | None:
    """Read an optional positive integer, allowing ``null`` for no limit."""
    value = data.get(field_name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            f"{context} field '{field_name}' must be a positive integer or null."
        )
    return value


def _optional_positive_int(
    data: Mapping[str, Any], field_name: str, context: str, *, default: int
) -> int:
    """Read an optional positive integer setting that cannot be null."""
    value = data.get(field_name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{context} field '{field_name}' must be a positive integer.")
    return value


def _optional_nonnegative_int(
    data: Mapping[str, Any], field_name: str, context: str, *, default: int
) -> int:
    """Read an optional integer setting that can be zero but never negative."""
    value = data.get(field_name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(
            f"{context} field '{field_name}' must be a non-negative integer."
        )
    return value


def _optional_string_tuple(data: Mapping[str, Any], field_name: str, context: str) -> tuple[str, ...]:
    """Read an optional list of strings as an immutable tuple."""
    value = data.get(field_name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{context} field '{field_name}' must be a list of strings.")
    return tuple(value)
