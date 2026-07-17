"""Future orchestration boundary for source-specific clip collectors.

Networking and source retrieval intentionally do not exist yet. This module is
reserved for composing validated configuration, source adapters, and storage.
"""

from __future__ import annotations

from .models import CollectorConfig


class ClipCollector:
    """Container for future source adapters and local collection workflow."""

    def __init__(self, config: CollectorConfig) -> None:
        """Keep validated configuration ready for a future collection run."""
        self.config = config
