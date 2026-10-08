"""Hosted pilot boundary: startup, caller isolation, retention, and egress."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from aeo_validator_service import app as app_module
from aeo_validator_service import fetcher
from aeo_validator_service.app import app
from aeo_validator_service.models import ValidationResult
from aeo_validator_service.request_guard import MAX_REQUEST_BYTES
from aeo_validator_service.sqlite_watch_store import SQLiteWatchStore
from aeo_validator_service.tenant_auth import TenantAuth

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
BODY: dict[str, Any] = {
    "aeo_version": "0.1",
    "entity": {"id": "x", "type": "Organization", "name": "X"},
    "authority": {"primary_sources": []},
}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hosted_environment(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("AEO_HOSTED_MODE", "1")
    monkeypatch.setenv(
        "AEO_TENANT_TOKEN_SHA256", json.dumps({"buyer-a": _digest(TOKEN_A), "buyer-b": _digest(TOKEN_B)})
    )
    monkeypatch.setenv("AEO_WATCH_DB_PATH", str(db_path))
    monkeypatch.setenv("AEO_FETCH_ALLOWED_HOSTS", "acme.example")
    monkeypatch.setenv("AEO_TENANT_ALLOWED_HOSTS", json.dumps({"buyer-a": ["acme.example"]}))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_hosted_startup_fails_closed_without_credentials_or_durable_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AEO_HOSTED_MODE", "1")
    monkeypatch.delenv("AEO_TENANT_TOKEN_SHA256", raising=False)
    with pytest.raises(ValueError, match="tenant-to-digest"):
        TenantAuth.from_environment()
    monkeypatch.setenv("AEO_TENANT_TOKEN_SHA256", json.dumps({"buyer-a": _digest(TOKEN_A)}))
    monkeypatch.delenv("AEO_WATCH_DB_PATH", raising=False)
    with pytest.raises(ValueError, match="absolute file path"):
        with TestClient(app):
            pass
    assert not list(tmp_path.iterdir())


def test_hosted_auth_isolation_persistence_and_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "watches.sqlite3"
    _hosted_environment(monkeypatch, db_path)
    real_client = httpx.AsyncClient
    upstream = {"body": BODY}

    async def public_dns(_host: str) -> str:
        return "93.184.215.14"

    monkeypatch.setattr(fetcher, "_check_public_dns", public_dns)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream["body"])

    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(app_module.httpx, "AsyncClient", factory)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/watches").status_code == 401
        assert client.get("/watches", headers=_auth("wrong" * 10)).status_code == 401
        # Authorization is checked before a malformed request body is parsed.
        assert (
            client.post("/validate/inline", content=b"not JSON", headers=_auth("wrong" * 10)).status_code
            == 401
        )
        assert client.post("/validate/inline", json={"body": BODY}, headers=_auth(TOKEN_A)).status_code == 200
        created = client.post(
            "/watches", json={"url": "https://acme.example/doc.json"}, headers=_auth(TOKEN_A)
        )
        assert created.status_code == 201
        watch_id = created.json()["watch_id"]
        assert client.get("/watches", headers=_auth(TOKEN_A)).json()["watch_ids"] == [watch_id]
        assert client.get("/watches", headers=_auth(TOKEN_B)).json()["watch_ids"] == []
        assert (
            client.post(
                "/validate/by-url", json={"url": "https://acme.example/doc.json"}, headers=_auth(TOKEN_B)
            ).status_code
            == 403
        )
        assert client.get(f"/watches/{watch_id}", headers=_auth(TOKEN_B)).status_code == 404
        assert client.post(f"/watches/{watch_id}/recheck", headers=_auth(TOKEN_B)).status_code == 404
        assert client.delete(f"/watches/{watch_id}", headers=_auth(TOKEN_B)).status_code == 204
        assert client.get(f"/watches/{watch_id}", headers=_auth(TOKEN_A)).status_code == 200
        oversize = b"x" * (MAX_REQUEST_BYTES + 1)
        assert client.post("/validate/inline", content=oversize, headers=_auth(TOKEN_A)).status_code == 413
        assert (
            client.post(
                "/validate/inline",
                content=iter([b"x" * MAX_REQUEST_BYTES, b"x"]),
                headers=_auth(TOKEN_A),
            ).status_code
            == 413
        )
    upstream["body"] = {**BODY, "entity": {**BODY["entity"], "name": "Changed"}}
    with TestClient(app) as client:
        persisted = client.get(f"/watches/{watch_id}", headers=_auth(TOKEN_A))
        assert persisted.status_code == 200
        assert persisted.json()["last_result"]["body"] is None
        assert client.get(f"/watches/{watch_id}/history", headers=_auth(TOKEN_A)).status_code == 200
        drift = client.post(f"/watches/{watch_id}/recheck", headers=_auth(TOKEN_A))
        assert drift.status_code == 200
        assert drift.json()["changed_fields"] == ["entity"]
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT tenant_id, action FROM audit_events ORDER BY id").fetchall()
        assert rows == [
            ("buyer-a", "validate_inline"),
            ("buyer-a", "created"),
            ("buyer-a", "initial_result"),
            ("buyer-a", "rechecked"),
        ]


def test_rotation_overlap_and_revocation_on_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _hosted_environment(monkeypatch, path)
    new_token = "c" * 64
    monkeypatch.setenv(
        "AEO_TENANT_TOKEN_SHA256", json.dumps({"buyer-a": [_digest(TOKEN_A), _digest(new_token)]})
    )
    with TestClient(app) as client:
        assert client.get("/watches", headers=_auth(TOKEN_A)).status_code == 200
        assert client.get("/watches", headers=_auth(new_token)).status_code == 200
    monkeypatch.setenv("AEO_TENANT_TOKEN_SHA256", json.dumps({"buyer-a": [_digest(new_token)]}))
    with TestClient(app) as client:
        assert client.get("/watches", headers=_auth(TOKEN_A)).status_code == 401
        assert client.get("/watches", headers=_auth(new_token)).status_code == 200


def test_sqlite_retention_deletes_expired_watch_history_and_audit(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = SQLiteWatchStore(str(path), retention_days=1)
    watch = store.create("https://acme.example/doc.json", tenant="buyer-a")
    store.record(
        watch.watch_id,
        ValidationResult(
            url=watch.url,
            fetched_at="2026-10-07T00:00:00+00:00",
            content_hash="sha256:" + "a" * 64,
            spec="aeo",
            valid=True,
            body=BODY,
        ),
        tenant="buyer-a",
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE watches SET expires_at = '2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE audit_events SET recorded_at = '2000-01-01T00:00:00+00:00'")
    with pytest.raises(KeyError):
        store.get(watch.watch_id, tenant="buyer-a")
    store.close()
    with sqlite3.connect(path) as db:
        for table in ("watches", "results", "audit_events"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_local_sqlite_backup_and_restore_drill(tmp_path: Path) -> None:
    live_path = tmp_path / "live.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    store = SQLiteWatchStore(str(live_path))
    watch = store.create("https://acme.example/doc.json", tenant="buyer-a")
    store.record(
        watch.watch_id,
        ValidationResult(
            url=watch.url,
            fetched_at="2026-10-07T00:00:00+00:00",
            content_hash="sha256:" + "b" * 64,
            spec="aeo",
            valid=True,
            body=BODY,
        ),
        tenant="buyer-a",
    )
    with sqlite3.connect(live_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    store.delete(watch.watch_id, tenant="buyer-a")
    assert store.list_ids(tenant="buyer-a") == []
    store.close()
    restored = SQLiteWatchStore(str(backup_path))
    assert restored.get(watch.watch_id, tenant="buyer-a").last_result is not None
    restored.close()


def test_atomic_watch_creation_rolls_back_if_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "state.sqlite3"
    store = SQLiteWatchStore(str(path))
    result = ValidationResult(
        url="https://acme.example/doc.json",
        fetched_at="2026-10-07T00:00:00+00:00",
        content_hash="sha256:" + "c" * 64,
        spec="aeo",
        valid=True,
        body=BODY,
    )

    def failing_audit(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated audit disk failure")

    monkeypatch.setattr(store, "_audit", failing_audit)
    with pytest.raises(OSError, match="audit disk failure"):
        store.create_with_result(result.url, result, tenant="buyer-a")
    assert store.list_ids(tenant="buyer-a") == []
    store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_fetch_connects_to_checked_ip_with_original_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEO_FETCH_ALLOWED_HOSTS", "vendor.example")

    async def public_dns(_host: str) -> str:
        return "93.184.215.14"

    monkeypatch.setattr(fetcher, "_check_public_dns", public_dns)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        body, _digest_value = await fetcher.fetch_and_parse(client, "https://vendor.example/doc.json")
    assert body == BODY
    assert str(seen[0].url) == "https://93.184.215.14/doc.json"
    assert seen[0].headers["host"] == "vendor.example"
    assert seen[0].extensions["sni_hostname"] == "vendor.example"


@pytest.mark.asyncio
async def test_pinned_httpcore_connection_uses_original_hostname_for_tls() -> None:
    """Exercise the pinned httpcore version's actual TCP and TLS handoff."""
    connected: list[tuple[str, int]] = []
    tls_names: list[str | None] = []

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self) -> None:
            self._response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            del max_bytes, timeout
            response, self._response = self._response, b""
            return response

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            del buffer, timeout

        async def aclose(self) -> None:
            return None

        async def start_tls(
            self, ssl_context: object, server_hostname: str | None = None, timeout: float | None = None
        ) -> httpcore.AsyncNetworkStream:
            del ssl_context, timeout
            tls_names.append(server_hostname)
            return self

        def get_extra_info(self, info: str) -> object:
            del info
            return None

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> httpcore.AsyncNetworkStream:
            del timeout, local_address, socket_options
            connected.append((host, port))
            return Stream()

        async def connect_unix_socket(
            self, path: str, timeout: float | None = None
        ) -> httpcore.AsyncNetworkStream:
            del path, timeout
            raise AssertionError("Unix socket should not be used")

        async def sleep(self, seconds: float) -> None:
            del seconds

    async with httpcore.AsyncConnectionPool(network_backend=Backend()) as pool:
        response = await pool.request(
            "GET",
            "https://93.184.215.14/doc.json",
            headers={"host": "vendor.example", "connection": "close"},
            extensions={"sni_hostname": "vendor.example"},
        )
        assert response.status == 200
    assert connected == [("93.184.215.14", 443)]
    assert tls_names == ["vendor.example"]
