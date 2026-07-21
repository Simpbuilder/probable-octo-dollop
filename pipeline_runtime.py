"""Local persistent status and background execution for Streamlit batch actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Literal
from uuid import uuid4


RuntimeState = Literal[
    "idle", "starting", "running", "completed", "failed", "cancelling", "cancelled"
]
ACTIVE_RUNTIME_STATES = frozenset({"starting", "running", "cancelling"})


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Durable, secret-free state for at most one local Streamlit batch job."""

    job_id: str | None = None
    stage: str | None = None
    status: RuntimeState = "idle"
    started_at: str | None = None
    completed_count: int = 0
    total_count: int = 0
    failed_count: int = 0
    current_item: str | None = None
    last_message: str = "Idle"
    finished_at: str | None = None
    cancel_requested: bool = False

    @property
    def is_active(self) -> bool:
        """Return whether a worker should still be considered active by the UI."""
        return self.status in ACTIVE_RUNTIME_STATES

    @classmethod
    def idle(cls) -> "RuntimeStatus":
        """Create the safe fallback used for missing or unreadable runtime state."""
        return cls()

    @classmethod
    def from_dict(cls, data: object) -> "RuntimeStatus":
        """Read a validated runtime status without allowing arbitrary JSON shapes."""
        if not isinstance(data, dict):
            raise ValueError("runtime status must be a JSON object")
        status = data.get("status", "idle")
        if status not in {
            "idle", "starting", "running", "completed", "failed", "cancelling", "cancelled"
        }:
            raise ValueError("runtime status has an unknown state")
        integer_fields = ("completed_count", "total_count", "failed_count")
        values: dict[str, object] = {field: data.get(field, 0) for field in integer_fields}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values.values()):
            raise ValueError("runtime counters must be non-negative integers")
        defaults = {"last_message": "Idle"}
        string_fields = ("job_id", "stage", "started_at", "current_item", "last_message", "finished_at")
        for field in string_fields:
            value = data.get(field, defaults.get(field))
            if value is not None and not isinstance(value, str):
                raise ValueError(f"runtime field {field} must be a string or null")
            values[field] = value
        cancel_requested = data.get("cancel_requested", False)
        if not isinstance(cancel_requested, bool):
            raise ValueError("runtime cancel_requested must be a boolean")
        return cls(status=status, cancel_requested=cancel_requested, **values)  # type: ignore[arg-type]


class RuntimeStatusStore:
    """Atomically read and update temporary local runtime status with a process-local lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> RuntimeStatus:
        """Return idle state for a missing, partial, or malformed temporary status file."""
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return RuntimeStatus.from_dict(data)
            except (OSError, json.JSONDecodeError, ValueError):
                return RuntimeStatus.idle()

    def write(self, status: RuntimeStatus) -> RuntimeStatus:
        """Replace the status file atomically so a refresh never sees partial JSON."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
            temporary_path.write_text(
                json.dumps(asdict(status), indent=2) + "\n", encoding="utf-8"
            )
            temporary_path.replace(self.path)
            return status

    def update(self, updater: Callable[[RuntimeStatus], RuntimeStatus]) -> RuntimeStatus:
        """Serialize a read-modify-write operation with cancellation-safe locking."""
        with self._lock:
            current = self.load()
            return self.write(updater(current))

    def request_cancel(self) -> RuntimeStatus:
        """Request a graceful stop; the running stage checks before starting another item."""
        def mark_cancelling(current: RuntimeStatus) -> RuntimeStatus:
            if not current.is_active:
                return current
            return replace(
                current,
                status="cancelling",
                cancel_requested=True,
                last_message="Stop requested. The current item may finish before the batch stops.",
            )

        return self.update(mark_cancelling)


def load_runtime_status(status_file: Path) -> RuntimeStatus:
    """Load one runtime status file through the canonical recovery-safe store."""
    return RuntimeStatusStore(status_file).load()


@dataclass(frozen=True, slots=True)
class QueueProgress:
    """An item-level progress event emitted by one existing queue implementation."""

    stage: str
    current_item: str | None
    completed_count: int
    total_count: int
    failed_count: int
    remaining_count: int
    message: str


QueueProgressCallback = Callable[[QueueProgress], bool | None]
JobRunner = Callable[["RuntimeJobContext"], int]


class RuntimeJobContext:
    """Safe progress and cancellation boundary passed into a background stage adapter."""

    def __init__(self, store: RuntimeStatusStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def report(self, progress: QueueProgress) -> bool:
        """Persist one item update and return whether the next item may start."""
        def apply(current: RuntimeStatus) -> RuntimeStatus:
            if current.job_id != self._job_id:
                return current
            return replace(
                current,
                status="cancelling" if current.cancel_requested else "running",
                stage=progress.stage,
                completed_count=progress.completed_count,
                total_count=progress.total_count,
                failed_count=progress.failed_count,
                current_item=progress.current_item,
                last_message=progress.message,
            )

        status = self._store.update(apply)
        return status.job_id == self._job_id and not status.cancel_requested

    def cancellation_requested(self) -> bool:
        """Check the latest durable request before a queue begins another item."""
        status = self._store.load()
        return status.job_id == self._job_id and status.cancel_requested


class BackgroundJobManager:
    """Run one local batch thread at a time while preserving observable durable status."""

    def __init__(self, store: RuntimeStatusStore) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start(self, stage: str, runner: JobRunner) -> RuntimeStatus:
        """Start one job or return the existing active job without creating a duplicate thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._store.load()
            existing = self._store.load()
            if existing.is_active:
                self._store.write(
                    replace(
                        existing,
                        status="failed",
                        finished_at=_timestamp(),
                        last_message="Recovered stale runtime status after the prior worker stopped.",
                    )
                )
            job_id = uuid4().hex
            status = self._store.write(
                RuntimeStatus(
                    job_id=job_id,
                    stage=stage,
                    status="starting",
                    started_at=_timestamp(),
                    last_message=f"Starting {stage}.",
                )
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(job_id, runner),
                name=f"viral-clip-{stage.replace(' ', '-')}",
                daemon=True,
            )
            self._thread.start()
            return status

    def request_cancel(self) -> RuntimeStatus:
        """Forward a stop request to the durable status store."""
        return self._store.request_cancel()

    def _run(self, job_id: str, runner: JobRunner) -> None:
        """Execute a runner once and always transition it to a terminal state."""
        context = RuntimeJobContext(self._store, job_id)
        self._store.update(
            lambda current: replace(current, status="running", last_message="Running.")
            if current.job_id == job_id
            else current
        )
        try:
            exit_code = runner(context)
            self._finish(job_id, exit_code)
        except Exception as error:
            self._store.update(
                lambda current: replace(
                    current,
                    status="cancelled" if current.cancel_requested else "failed",
                    finished_at=_timestamp(),
                    current_item=None,
                    last_message=f"Background job failed: {error}",
                )
                if current.job_id == job_id
                else current
            )

    def _finish(self, job_id: str, exit_code: int) -> None:
        """Store a terminal result without hiding successful work already persisted by the stage."""
        def complete(current: RuntimeStatus) -> RuntimeStatus:
            if current.job_id != job_id:
                return current
            if current.cancel_requested:
                state: RuntimeState = "cancelled"
                message = "Batch stopped after the current completed item."
            elif exit_code == 0:
                state = "completed"
                message = "Batch completed."
            else:
                state = "failed"
                message = f"Batch finished with exit code {exit_code}."
            return replace(
                current,
                status=state,
                current_item=None,
                finished_at=_timestamp(),
                last_message=message,
            )

        self._store.update(complete)


def _timestamp() -> str:
    """Return a compact timezone-aware timestamp suitable for persistent local status."""
    return datetime.now(timezone.utc).isoformat()
