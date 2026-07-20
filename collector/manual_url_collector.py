"""Local intake queue for manually supplied public URLs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .models import ClipMetadata
from .storage import DuplicateClipError, clip_exists, save_clip_metadata


class InvalidManualUrlError(ValueError):
    """Raised when a queue entry is not a syntactically valid HTTP(S) URL."""


@dataclass(frozen=True, slots=True)
class ManualUrl:
    """A validated queue URL with stable normalized and source representations."""

    original_url: str
    normalized_url: str
    is_reddit_url: bool
    subreddit: str | None


@dataclass(slots=True)
class ManualUrlSummary:
    """Counters describing one manual URL queue intake run."""

    urls_found: int = 0
    accepted: int = 0
    duplicates: int = 0
    invalid_urls: int = 0
    errors: int = 0
    eligible: int = 0
    processing: int = 0
    remaining: int = 0


class ManualUrlCollector:
    """Import queue entries as metadata without resolving or downloading media."""

    def __init__(
        self,
        input_file: Path,
        processed_file: Path,
        metadata_file: Path,
        *,
        maximum_urls_per_run: int = 50,
        clock: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Set up local queue and metadata paths with injectable test dependencies."""
        self._input_file = Path(input_file)
        self._processed_file = Path(processed_file)
        self._metadata_file = Path(metadata_file)
        if maximum_urls_per_run <= 0:
            raise ValueError("maximum_urls_per_run must be greater than zero.")
        self._maximum_urls_per_run = maximum_urls_per_run
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._logger = logger or logging.getLogger(__name__)

    def collect(self, *, process_all: bool = False) -> ManualUrlSummary:
        """Process queue entries while retaining invalid or failed lines for retry."""
        summary = ManualUrlSummary()
        try:
            input_lines = self._input_file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            summary.errors += 1
            self._logger.error("Could not read manual URL queue %s: %s", self._input_file, error)
            return summary

        queue_entries = [line for line in input_lines if line.strip() and not line.strip().startswith("#")]
        summary.urls_found = len(queue_entries)
        summary.eligible = len(queue_entries)
        summary.processing = len(queue_entries) if process_all else min(
            len(queue_entries), self._maximum_urls_per_run
        )
        summary.remaining = summary.eligible - summary.processing

        remaining_lines: list[str] = []
        completed_urls: list[str] = []
        processed_entries = 0
        for line in input_lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("#"):
                remaining_lines.append(line)
                continue

            if processed_entries >= summary.processing:
                remaining_lines.append(line)
                continue
            processed_entries += 1
            try:
                manual_url = normalize_manual_url(stripped_line)
                clip = create_manual_clip_metadata(manual_url, added_at=self._clock())
                if clip_exists(self._metadata_file, clip):
                    summary.duplicates += 1
                    completed_urls.append(manual_url.original_url)
                    continue

                save_clip_metadata(self._metadata_file, clip)
                summary.accepted += 1
                completed_urls.append(manual_url.original_url)
            except InvalidManualUrlError as error:
                summary.invalid_urls += 1
                remaining_lines.append(line)
                self._logger.debug("Keeping invalid URL for correction: %s", error)
            except DuplicateClipError:
                summary.duplicates += 1
                completed_urls.append(stripped_line)
            except Exception as error:
                summary.errors += 1
                remaining_lines.append(line)
                self._logger.error("Keeping failed URL for retry: %s (%s)", stripped_line, error)

        if completed_urls:
            self._finalize_completed_urls(completed_urls, remaining_lines, summary)
        return summary

    def _finalize_completed_urls(
        self,
        completed_urls: list[str],
        remaining_lines: list[str],
        summary: ManualUrlSummary,
    ) -> None:
        """Append completed entries to the audit log before removing them from input."""
        try:
            self._processed_file.parent.mkdir(parents=True, exist_ok=True)
            with self._processed_file.open("a", encoding="utf-8", newline="\n") as processed_file:
                for url in completed_urls:
                    processed_file.write(f"{url}\n")
            _write_queue_lines(self._input_file, remaining_lines)
        except OSError as error:
            summary.errors += 1
            self._logger.error(
                "Could not finalize processed URLs; queue entries were retained: %s", error
            )


def normalize_manual_url(raw_url: str) -> ManualUrl:
    """Validate and normalize an HTTP(S) URL without making a network request."""
    original_url = raw_url.strip()
    try:
        parsed = urlsplit(original_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidManualUrlError("URL has an invalid host or port.") from error

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise InvalidManualUrlError("URL must use the http or https scheme.")
    if not hostname or any(character.isspace() for character in hostname):
        raise InvalidManualUrlError("URL must include a valid host.")
    if parsed.username or parsed.password:
        raise InvalidManualUrlError("URL must not include credentials.")

    hostname = hostname.lower().rstrip(".")
    is_reddit_url = _is_reddit_host(hostname)
    if is_reddit_url and hostname in {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "new.reddit.com",
        "np.reddit.com",
    }:
        hostname = "www.reddit.com"

    if ":" in hostname and not hostname.startswith("["):
        host_with_port = f"[{hostname}]"
    else:
        host_with_port = hostname
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host_with_port = f"{host_with_port}:{port}"

    path = parsed.path or "/"
    if is_reddit_url and path != "/":
        path = path.rstrip("/") or "/"
    normalized_url = urlunsplit((scheme, host_with_port, path, parsed.query, ""))
    return ManualUrl(
        original_url=original_url,
        normalized_url=normalized_url,
        is_reddit_url=is_reddit_url,
        subreddit=_subreddit_from_path(path) if is_reddit_url else None,
    )


def create_manual_clip_metadata(manual_url: ManualUrl, added_at: datetime) -> ClipMetadata:
    """Create metadata for a validated manual URL without claiming media details."""
    digest = hashlib.sha256(manual_url.normalized_url.encode("utf-8")).hexdigest()
    source_label = "Reddit URL" if manual_url.is_reddit_url else "Manual URL"
    return ClipMetadata(
        unique_id=f"manual-{digest}",
        source="manual",
        subreddit=manual_url.subreddit,
        source_post_id=digest,
        source_url=manual_url.original_url,
        title=f"{source_label}: {manual_url.normalized_url}",
        author="manual_intake",
        score=0,
        comment_count=0,
        created_at=added_at,
        media_url=None,
        local_file_path=None,
        download_status="pending",
        processing_status="pending",
        added_at=added_at,
    )


def _is_reddit_host(hostname: str) -> bool:
    """Recognize Reddit domains without resolving or fetching the URL."""
    return hostname in {"reddit.com", "redd.it"} or hostname.endswith(".reddit.com")


def _subreddit_from_path(path: str) -> str | None:
    """Extract a subreddit name when a Reddit path contains ``/r/<name>``."""
    path_parts = [part for part in path.split("/") if part]
    for index, part in enumerate(path_parts[:-1]):
        if part.lower() == "r" and path_parts[index + 1]:
            return path_parts[index + 1]
    return None


def _write_queue_lines(input_file: Path, lines: list[str]) -> None:
    """Atomically rewrite the queue with only unresolved, blank, and comment lines."""
    content = "\n".join(lines)
    if lines:
        content += "\n"
    temporary_file = input_file.with_suffix(f"{input_file.suffix}.tmp")
    temporary_file.write_text(content, encoding="utf-8", newline="\n")
    temporary_file.replace(input_file)
