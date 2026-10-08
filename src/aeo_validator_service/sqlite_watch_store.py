"""Single-node durable watch state with tenant scoping and bounded retention.

The SQLite file contains the latest fetched document body for drift comparison.
Use synthetic/public documents for the hosted pilot; customer data needs an
approved storage location, volume encryption, backup, and deletion policy.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import cast

from .models import SpecKind, ValidationResult, Watch
from .watch_store import MAX_HISTORY, MAX_WATCHES


class SQLiteWatchStore:
    """One SQLite file shared by one service process."""

    def __init__(self, path: str, retention_days: int = 7) -> None:
        if not Path(path).is_absolute() or path == ":memory:":
            raise ValueError("AEO_WATCH_DB_PATH must be an absolute file path")
        if not 1 <= retention_days <= 30:
            raise ValueError("AEO_WATCH_RETENTION_DAYS must be 1 to 30")
        if not Path(path).parent.is_dir():
            raise ValueError("AEO_WATCH_DB_PATH parent directory must exist")
        self.retention_days = retention_days
        self._lock = Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    spec_hint TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_watches_tenant ON watches(tenant_id);
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(watch_id) ON DELETE CASCADE,
                    recorded_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_results_watch ON results(watch_id, id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    watch_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    content_hash TEXT,
                    valid INTEGER
                );
                """
            )
        with self._lock, self._db:
            self._purge()

    def close(self) -> None:
        self._db.close()

    def _purge(self) -> None:
        now = datetime.now(UTC)
        self._db.execute("DELETE FROM watches WHERE expires_at <= ?", (now.isoformat(timespec="seconds"),))
        cutoff = (now - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        self._db.execute("DELETE FROM audit_events WHERE recorded_at <= ?", (cutoff,))
        # Keep expiry durable even when the subsequent lookup returns 404.
        self._db.commit()

    def has_capacity(self, tenant: str = "local") -> bool:
        del tenant  # Keep the global 16-watch memory/disk cap across all tenants.
        with self._lock, self._db:
            self._purge()
            count = self._db.execute("SELECT count(*) FROM watches").fetchone()[0]
            return bool(count < MAX_WATCHES)

    def create(self, url: str, *, spec_hint: str | None = None, tenant: str = "local") -> Watch:
        watch_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._purge()
            count = self._db.execute("SELECT count(*) FROM watches").fetchone()[0]
            if count >= MAX_WATCHES:
                raise OverflowError("watch limit reached")
            self._db.execute(
                "INSERT INTO watches VALUES (?, ?, ?, ?, ?, ?)",
                (watch_id, tenant, url, spec_hint, created_at, expires_at),
            )
            self._audit(tenant, watch_id, "created", created_at)
        return Watch(
            watch_id=watch_id,
            url=url,
            spec_hint=cast(SpecKind | None, spec_hint),
            last_result=None,
            history_count=0,
            created_at=created_at,
        )

    def create_with_result(
        self,
        url: str,
        result: ValidationResult,
        *,
        spec_hint: str | None = None,
        tenant: str = "local",
    ) -> Watch:
        """Atomically persist the new watch, first result, and audit rows."""
        watch_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._purge()
            count = self._db.execute("SELECT count(*) FROM watches").fetchone()[0]
            if count >= MAX_WATCHES:
                raise OverflowError("watch limit reached")
            self._db.execute(
                "INSERT INTO watches VALUES (?, ?, ?, ?, ?, ?)",
                (watch_id, tenant, url, spec_hint, created_at, expires_at),
            )
            self._db.execute(
                "INSERT INTO results(watch_id, recorded_at, result_json) VALUES (?, ?, ?)",
                (watch_id, created_at, result.model_dump_json()),
            )
            self._audit(tenant, watch_id, "created", created_at)
            self._audit(tenant, watch_id, "initial_result", created_at, result)
            return self._build_watch(self._watch_row(watch_id, tenant))

    def _audit(
        self,
        tenant: str,
        watch_id: str,
        action: str,
        recorded_at: str,
        result: ValidationResult | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO audit_events(tenant_id, watch_id, action, recorded_at, content_hash, valid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                tenant,
                watch_id,
                action,
                recorded_at,
                result.content_hash if result else None,
                int(result.valid) if result else None,
            ),
        )

    def audit_validation(self, tenant: str, action: str, result: ValidationResult) -> None:
        if action not in {"validate_inline", "validate_by_url"}:
            raise ValueError("invalid validation audit action")
        with self._lock, self._db:
            self._purge()
            self._audit(tenant, "", action, datetime.now(UTC).isoformat(timespec="seconds"), result)

    def _watch_row(self, watch_id: str, tenant: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM watches WHERE watch_id = ? AND tenant_id = ?", (watch_id, tenant)
        ).fetchone()
        if row is None:
            raise KeyError("unknown watch_id")
        return cast(sqlite3.Row, row)

    def _result_rows(self, watch_id: str) -> list[sqlite3.Row]:
        return list(
            self._db.execute(
                "SELECT id, result_json FROM results WHERE watch_id = ? ORDER BY id", (watch_id,)
            )
        )

    def _build_watch(self, row: sqlite3.Row) -> Watch:
        results = self._result_rows(str(row["watch_id"]))
        last = ValidationResult.model_validate_json(results[-1]["result_json"]) if results else None
        return Watch(
            watch_id=row["watch_id"],
            url=row["url"],
            spec_hint=row["spec_hint"],
            created_at=row["created_at"],
            history_count=len(results),
            last_result=last,
        )

    def record(self, watch_id: str, result: ValidationResult, *, tenant: str = "local") -> Watch:
        with self._lock, self._db:
            self._purge()
            row = self._watch_row(watch_id, tenant)
            previous = self._db.execute(
                "SELECT id, result_json FROM results WHERE watch_id = ? ORDER BY id DESC LIMIT 1", (watch_id,)
            ).fetchone()
            if previous is not None:
                old = ValidationResult.model_validate_json(previous["result_json"])
                self._db.execute(
                    "UPDATE results SET result_json = ? WHERE id = ?",
                    (old.model_copy(update={"body": None}).model_dump_json(), previous["id"]),
                )
            now = datetime.now(UTC).isoformat(timespec="seconds")
            self._db.execute(
                "INSERT INTO results(watch_id, recorded_at, result_json) VALUES (?, ?, ?)",
                (watch_id, now, result.model_dump_json()),
            )
            self._db.execute(
                "DELETE FROM results WHERE watch_id = ? AND id NOT IN "
                "(SELECT id FROM results WHERE watch_id = ? ORDER BY id DESC LIMIT ?)",
                (watch_id, watch_id, MAX_HISTORY),
            )
            self._audit(tenant, watch_id, "rechecked" if previous else "initial_result", now, result)
            return self._build_watch(row)

    def get(self, watch_id: str, *, tenant: str = "local") -> Watch:
        with self._lock, self._db:
            self._purge()
            return self._build_watch(self._watch_row(watch_id, tenant))

    def history(self, watch_id: str, *, tenant: str = "local") -> list[ValidationResult]:
        with self._lock, self._db:
            self._purge()
            self._watch_row(watch_id, tenant)
            return [
                ValidationResult.model_validate_json(row["result_json"])
                for row in self._result_rows(watch_id)
            ]

    def previous(self, watch_id: str, *, tenant: str = "local") -> ValidationResult | None:
        with self._lock, self._db:
            self._purge()
            self._watch_row(watch_id, tenant)
            row = self._db.execute(
                "SELECT result_json FROM results WHERE watch_id = ? ORDER BY id DESC LIMIT 1", (watch_id,)
            ).fetchone()
            return ValidationResult.model_validate_json(row["result_json"]) if row else None

    def list_ids(self, *, tenant: str = "local") -> list[str]:
        with self._lock, self._db:
            self._purge()
            rows = self._db.execute(
                "SELECT watch_id FROM watches WHERE tenant_id = ? ORDER BY created_at, watch_id", (tenant,)
            )
            return [str(row["watch_id"]) for row in rows]

    def delete(self, watch_id: str, *, tenant: str = "local") -> None:
        with self._lock, self._db:
            self._purge()
            deleted = self._db.execute(
                "DELETE FROM watches WHERE watch_id = ? AND tenant_id = ?", (watch_id, tenant)
            )
            if deleted.rowcount:
                self._audit(tenant, watch_id, "deleted", datetime.now(UTC).isoformat(timespec="seconds"))
