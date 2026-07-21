"""Refresh policy for the local Streamlit runtime display."""

from __future__ import annotations

from typing import Final, Mapping

from pipeline_runtime import RuntimeStatus


AUTO_REFRESH_INTERVALS: Final[Mapping[str, int | None]] = {
    "1 second": 1,
    "2 seconds": 2,
    "5 seconds": 5,
    "Off": None,
}


def resolve_auto_refresh_interval(selection: str, status: RuntimeStatus) -> int | None:
    """Return a configured interval only while a local background job is active.

    ``Off`` always disables refresh. Unknown values are rejected so a malformed UI state
    cannot silently select an unintended polling interval.
    """
    if selection not in AUTO_REFRESH_INTERVALS:
        raise ValueError("Unknown live refresh interval.")
    return AUTO_REFRESH_INTERVALS[selection] if status.is_active else None
