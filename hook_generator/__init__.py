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
from .diagnostics import HookFlowDebug, inspect_hook_flow
from .review import HookReviewSummary, HookReviewer

__all__ = [
    "create_openai_hook_client",
    "HookGenerationClientError",
    "HookGenerationCredentialsError",
    "HookGenerationDependencyError",
    "HookGenerationResponseError",
    "HookGenerationSummary",
    "HookFlowDebug",
    "HookReviewSummary",
    "HookReviewer",
    "load_openai_api_key",
    "inspect_hook_flow",
    "parse_hook_candidates",
    "PendingHookGenerator",
]
