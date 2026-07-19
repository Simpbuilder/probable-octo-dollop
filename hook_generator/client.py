"""Lazy OpenAI Responses API access and API-key loading for hook generation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class HookGenerationClientError(RuntimeError):
    """Base error raised for a recoverable hook-generation API problem."""


class HookGenerationCredentialsError(HookGenerationClientError):
    """Raised when the OpenAI key is not available to start a generation run."""


class HookGenerationDependencyError(HookGenerationClientError):
    """Raised when the optional OpenAI SDK has not been installed."""


class OpenAIResponsesProtocol(Protocol):
    """The narrow SDK surface used by this project and easily mocked in tests."""

    def create(self, **kwargs: object) -> object:
        """Create one text-only Responses API request."""


class HookGenerationClientProtocol(Protocol):
    """The API adapter surface consumed by hook queue orchestration."""

    def generate(self, *, model: str, instructions: str, input_text: str) -> str:
        """Return the raw text response for one metadata-backed hook request."""


def load_openai_api_key(
    env_path: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Load ``OPENAI_API_KEY`` from the root environment without overriding deployments."""
    env_path = Path(env_path)
    if env_path.exists():
        try:
            from dotenv import load_dotenv
        except ModuleNotFoundError as error:
            raise HookGenerationDependencyError(
                "python-dotenv is not installed. Run: pip install -r requirements.txt"
            ) from error
        load_dotenv(dotenv_path=env_path, override=False)

    environment = os.environ if environ is None else environ
    api_key = environment.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HookGenerationCredentialsError(
            "Missing OPENAI_API_KEY. Add it to .env using .env.example as a template."
        )
    return api_key


def create_openai_hook_client(api_key: str) -> "OpenAIHookClient":
    """Build an OpenAI Responses API adapter only for an explicit generation run."""
    try:
        from openai import OpenAI
    except ModuleNotFoundError as error:
        raise HookGenerationDependencyError(
            "openai is not installed. Run: pip install -r requirements.txt"
        ) from error
    return OpenAIHookClient(OpenAI(api_key=api_key).responses)


class OpenAIHookClient:
    """Small wrapper around the official SDK's text Responses API."""

    def __init__(self, responses: OpenAIResponsesProtocol) -> None:
        """Accept an injectable responses endpoint for narrow, offline tests."""
        self._responses = responses

    def generate(self, *, model: str, instructions: str, input_text: str) -> str:
        """Request one JSON-only candidate payload without video or media inputs."""
        try:
            response = self._responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
                max_output_tokens=180,
            )
        except Exception as error:
            raise HookGenerationClientError("OpenAI hook generation request failed.") from error

        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise HookGenerationClientError("OpenAI returned no usable hook text.")
        return output_text
