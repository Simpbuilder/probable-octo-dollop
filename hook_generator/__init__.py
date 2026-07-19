"""OpenAI hook-candidate generation and local metadata review tools."""

from .client import (
    HookGenerationClientError,
    HookGenerationCredentialsError,
    HookGenerationDependencyError,
    create_openai_hook_client,
    load_openai_api_key,
)
from .generator import (
    HookGenerationResponseError,
    HookGenerationSummary,
    PendingHookGenerator,
    parse_hook_candidates,
)
from .review import HookReviewSummary, HookReviewer

__all__ = [
    "create_openai_hook_client",
    "HookGenerationClientError",
    "HookGenerationCredentialsError",
    "HookGenerationDependencyError",
    "HookGenerationResponseError",
    "HookGenerationSummary",
    "HookReviewSummary",
    "HookReviewer",
    "load_openai_api_key",
    "parse_hook_candidates",
    "PendingHookGenerator",
]
