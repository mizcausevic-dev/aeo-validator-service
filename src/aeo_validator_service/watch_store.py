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

from .fetcher import now_iso
from .models import ValidationResult, Watch


class WatchStore:
    """Thread-safe in-memory watch + result history."""

    __slots__ = ("_history", "_lock", "_watches")

    def __init__(self) -> None:
        self._watches: dict[str, Watch] = {}
        self._history: dict[str, list[ValidationResult]] = {}
        self._lock = Lock()

    def create(self, url: str, *, spec_hint: str | None = None) -> Watch:
        watch_id = uuid.uuid4().hex[:12]
        with self._lock:
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

    def record(self, watch_id: str, result: ValidationResult) -> Watch:
        """Append a validation result and update the watch metadata."""
        with self._lock:
            try:
                watch = self._watches[watch_id]
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err
            self._history[watch_id].append(result)
            updated = watch.model_copy(
                update={"last_result": result, "history_count": len(self._history[watch_id])}
            )
            self._watches[watch_id] = updated
        return updated

    def get(self, watch_id: str) -> Watch:
        with self._lock:
            try:
                return self._watches[watch_id]
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err

    def history(self, watch_id: str) -> list[ValidationResult]:
        with self._lock:
            try:
                return list(self._history[watch_id])
            except KeyError as err:
                raise KeyError(f"unknown watch_id: {watch_id!r}") from err

    def previous(self, watch_id: str) -> ValidationResult | None:
        with self._lock:
            hist = self._history.get(watch_id) or []
            return hist[-1] if hist else None

    def list_ids(self) -> list[str]:
        with self._lock:
            return list(self._watches.keys())

    def delete(self, watch_id: str) -> None:
        with self._lock:
            self._watches.pop(watch_id, None)
            self._history.pop(watch_id, None)
