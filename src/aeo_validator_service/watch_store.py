"""
In-memory watch store.

Each watch is a `(watch_id, url, history[])` triple. The first POST /watches
fetches + validates + stores the initial result; later POST /watches/{id}/recheck
calls fetch again and append.

A real deployment would back this with Postgres or DynamoDB; the protocol the
app uses is small enough that swapping it is mechanical.
"""

from __future__ import annotations

import uuid
from threading import Lock

from .drift import compute_drift
from .fetcher import now_iso
from .models import DriftReport, ValidationResult, Watch

MAX_WATCHES = 16
MAX_HISTORY = 20


class WatchStore:
    """Thread-safe in-memory watch + result history."""

    __slots__ = ("_history", "_lock", "_watches")

    def __init__(self) -> None:
        self._watches: dict[str, Watch] = {}
        self._history: dict[str, list[ValidationResult]] = {}
        self._lock = Lock()

    def has_capacity(self, tenant: str = "local") -> bool:
        """Avoid a fetch when the process-local watch store is already full."""
        del tenant
        with self._lock:
            return len(self._watches) < MAX_WATCHES

    def create(self, url: str, *, spec_hint: str | None = None, tenant: str = "local") -> Watch:
        del tenant
        watch_id = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._watches) >= MAX_WATCHES:
                raise OverflowError("watch limit reached")
            watch = Watch(
                watch_id=watch_id,
                url=url,
                spec_hint=spec_hint,  # type: ignore[arg-type]
                last_result=None,
                history_count=0,
                created_at=now_iso(),
            )
            self._watches[watch_id] = watch
            self._history[watch_id] = []
        return watch

    def record(self, watch_id: str, result: ValidationResult, *, tenant: str = "local") -> Watch:
        """Append a validation result and update the watch metadata."""
        del tenant
        with self._lock:
            try:
                watch = self._watches[watch_id]
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err
            history = self._history[watch_id]
            if history:
                history[-1] = history[-1].model_copy(update={"body": None})
            history.append(result)
            if len(history) > MAX_HISTORY:
                history.pop(0)
            updated = watch.model_copy(update={"last_result": result, "history_count": len(history)})
            self._watches[watch_id] = updated
        return updated

    def record_and_diff(
        self, watch_id: str, result: ValidationResult, *, tenant: str = "local"
    ) -> DriftReport:
        """Compare and append under one lock so concurrent rechecks see ordered history."""
        del tenant
        with self._lock:
            try:
                watch = self._watches[watch_id]
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err
            history = self._history[watch_id]
            previous = history[-1] if history else None
            drift = compute_drift(previous, result)
            if history:
                history[-1] = history[-1].model_copy(update={"body": None})
            history.append(result)
            if len(history) > MAX_HISTORY:
                history.pop(0)
            self._watches[watch_id] = watch.model_copy(
                update={"last_result": result, "history_count": len(history)}
            )
            return drift

    def get(self, watch_id: str, *, tenant: str = "local") -> Watch:
        del tenant
        with self._lock:
            try:
                return self._watches[watch_id]
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err

    def history(self, watch_id: str, *, tenant: str = "local") -> list[ValidationResult]:
        del tenant
        with self._lock:
            try:
                return list(self._history[watch_id])
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err

    def previous(self, watch_id: str, *, tenant: str = "local") -> ValidationResult | None:
        del tenant
        with self._lock:
            hist = self._history.get(watch_id) or []
            return hist[-1] if hist else None

    def list_ids(self, *, tenant: str = "local") -> list[str]:
        del tenant
        with self._lock:
            return list(self._watches.keys())

    def delete(self, watch_id: str, *, tenant: str = "local") -> None:
        del tenant
        with self._lock:
            self._watches.pop(watch_id, None)
            self._history.pop(watch_id, None)
