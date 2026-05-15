"""End-to-end tests for the FastAPI app. Mocks vendor URLs via httpx.MockTransport."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aeo_validator_service import app as app_module
from aeo_validator_service.app import app

SAMPLE_AEO: dict[str, Any] = {
    "aeo_version": "0.1",
    "entity": {
        "id": "https://acme.example/#org",
        "type": "Organization",
        "name": "Acme",
    },
    "authority": {"primary_sources": ["https://acme.example/"]},
    "claims": [],
}

SAMPLE_AEO_V2: dict[str, Any] = {
    "aeo_version": "0.1",
    "entity": {
        "id": "https://acme.example/#org",
        "type": "Organization",
        "name": "Acme Holdings, Inc.",
    },
    "authority": {"primary_sources": ["https://acme.example/"]},
    "claims": [{"id": "tagline", "predicate": "description", "value": "AI tutoring"}],
}


def _make_router(initial: dict[str, Any]) -> tuple[httpx.MockTransport, dict[str, dict[str, Any]]]:
    """Build a mock transport whose payload can be flipped by the test."""
    state = {"current": initial}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/aeo.json"):
            return httpx.Response(200, json=state["current"])
        if url.endswith("/.well-known/missing.json"):
            return httpx.Response(404)
        if url.endswith("/.well-known/bad-json.json"):
            return httpx.Response(200, content=b"not JSON {", headers={"content-type": "application/json"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), state


@pytest.fixture
def client_with_aeo(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, dict[str, Any]]]:
    """TestClient + a handle to swap the mocked AEO payload mid-test."""
    transport, state = _make_router(SAMPLE_AEO)
    real_async_client = httpx.AsyncClient

    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, follow_redirects=True)

    monkeypatch.setattr(app_module.httpx, "AsyncClient", factory)
    with TestClient(app) as c:
        yield c, state


class TestMeta:
    def test_root(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["name"] == "aeo-validator-service"

    def test_healthz(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        assert client.get("/healthz").json() == {"status": "ok"}


class TestValidateByUrl:
    def test_happy_path(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post(
            "/validate/by-url",
            json={"url": "https://acme.example/.well-known/aeo.json"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["spec"] == "aeo"
        assert body["content_hash"].startswith("sha256:")
        assert body["body"] is None  # include_body=False default

    def test_include_body(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post(
            "/validate/by-url",
            json={"url": "https://acme.example/.well-known/aeo.json", "include_body": True},
        )
        body = r.json()
        assert body["body"]["entity"]["name"] == "Acme"

    def test_missing_url_is_400(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post(
            "/validate/by-url",
            json={"url": "https://acme.example/.well-known/missing.json"},
        )
        assert r.status_code == 400
        assert "HTTP 404" in r.json()["detail"]

    def test_bad_json_is_400(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post(
            "/validate/by-url",
            json={"url": "https://acme.example/.well-known/bad-json.json"},
        )
        assert r.status_code == 400


class TestValidateInline:
    def test_inline_valid_doc(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post("/validate/inline", json={"body": SAMPLE_AEO})
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_inline_invalid_doc(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post(
            "/validate/inline",
            json={"body": {"aeo_version": "0.1", "entity": {}}},
        )
        assert r.json()["valid"] is False


class TestWatchLifecycle:
    def test_create_and_recheck_no_drift(
        self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]
    ) -> None:
        client, _state = client_with_aeo
        r = client.post("/watches", json={"url": "https://acme.example/.well-known/aeo.json"})
        assert r.status_code == 201
        watch_id = r.json()["watch_id"]

        # Re-check immediately; payload unchanged.
        r2 = client.post(f"/watches/{watch_id}/recheck")
        assert r2.status_code == 200
        drift = r2.json()
        assert drift["drifted"] is False
        assert drift["content_hash_before"] == drift["content_hash_after"]

    def test_recheck_detects_drift(
        self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]
    ) -> None:
        client, state = client_with_aeo
        r = client.post("/watches", json={"url": "https://acme.example/.well-known/aeo.json"})
        watch_id = r.json()["watch_id"]

        # Swap the upstream payload.
        state["current"] = SAMPLE_AEO_V2

        r2 = client.post(f"/watches/{watch_id}/recheck")
        drift = r2.json()
        assert drift["drifted"] is True
        assert drift["content_hash_before"] != drift["content_hash_after"]
        # `entity` and `claims` changed.
        assert "claims" in drift["changed_fields"] or "claims" in drift["added_fields"]

    def test_history_grows(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, state = client_with_aeo
        r = client.post("/watches", json={"url": "https://acme.example/.well-known/aeo.json"})
        watch_id = r.json()["watch_id"]
        state["current"] = SAMPLE_AEO_V2
        client.post(f"/watches/{watch_id}/recheck")

        h = client.get(f"/watches/{watch_id}/history").json()
        assert len(h) == 2

    def test_list_and_get(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post("/watches", json={"url": "https://acme.example/.well-known/aeo.json"})
        watch_id = r.json()["watch_id"]
        ids = client.get("/watches").json()["watch_ids"]
        assert watch_id in ids
        assert client.get(f"/watches/{watch_id}").status_code == 200

    def test_recheck_unknown_watch_404(
        self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]
    ) -> None:
        client, _ = client_with_aeo
        assert client.post("/watches/missing/recheck").status_code == 404

    def test_delete_watch(self, client_with_aeo: tuple[TestClient, dict[str, dict[str, Any]]]) -> None:
        client, _ = client_with_aeo
        r = client.post("/watches", json={"url": "https://acme.example/.well-known/aeo.json"})
        watch_id = r.json()["watch_id"]
        del_r = client.delete(f"/watches/{watch_id}")
        assert del_r.status_code == 204
        assert client.get(f"/watches/{watch_id}").status_code == 404
