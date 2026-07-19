"""Interactively select or customize saved hook candidates without rendering video."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from collector import ConfigurationError, load_collector_config
from hook_generator import HookReviewer


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse an optional project-root override for local metadata review."""
    parser = argparse.ArgumentParser(description="Review saved hook candidates for collected clips.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Project root containing config/ and metadata/.",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run an interactive review without starting collection, downloads, or formatting."""
    parsed_arguments = parse_arguments(arguments)
    project_root = parsed_arguments.project_root.resolve()
    try:
        config = load_collector_config(project_root / "config")
    except ConfigurationError as error:
        print(f"Hook review not started: {error}")
        return 2
    if config.hook_generation_config is None:
        print("Hook review not started: hook generation configuration is missing.")
        return 2

    print(f"Metadata file: {config.metadata_file.resolve()}")
    summary = HookReviewer(config.metadata_file, config.hook_generation_config).run()
    print("Hook review")
    print(f"Available: {summary.available}")
    print(f"Selected: {summary.selected}")
    print(f"Custom: {summary.custom}")
    print(f"Skipped: {summary.skipped}")
    print(f"Rejected: {summary.rejected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
