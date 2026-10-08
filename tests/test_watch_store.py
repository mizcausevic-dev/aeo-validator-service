"""Memory and retention boundaries for process-local watches."""

from __future__ import annotations

import pytest

from aeo_validator_service.models import ValidationResult
from aeo_validator_service.watch_store import MAX_HISTORY, WatchStore


def _result(n: int) -> ValidationResult:
    return ValidationResult(
        url="https://vendor.example/doc.json",
        fetched_at="2026-10-07T00:00:00+00:00",
        content_hash=f"sha256:{n:064x}",
        spec="aeo",
        valid=True,
        body={"aeo_version": "0.1", "n": n},
    )


def test_history_is_bounded_and_only_latest_keeps_document_body() -> None:
    store = WatchStore()
    watch = store.create("https://vendor.example/doc.json")
    for n in range(MAX_HISTORY + 3):
        store.record(watch.watch_id, _result(n))
    history = store.history(watch.watch_id)
    assert len(history) == MAX_HISTORY
    assert all(item.body is None for item in history[:-1])
    assert history[-1].body is not None


def test_watch_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from aeo_validator_service import watch_store

    monkeypatch.setattr(watch_store, "MAX_WATCHES", 1)
    store = WatchStore()
    store.create("https://vendor.example/one")
    with pytest.raises(OverflowError, match="watch limit"):
        store.create("https://vendor.example/two")
