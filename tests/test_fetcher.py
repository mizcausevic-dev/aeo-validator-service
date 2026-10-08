"""Network boundary and parser regression tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from aeo_validator_service import fetcher


@pytest.mark.asyncio
async def test_network_fetch_is_disabled_without_exact_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEO_FETCH_ALLOWED_HOSTS", raising=False)
    with pytest.raises(fetcher.FetchError, match="not allowed"):
        await fetcher._check_url("https://vendor.example/doc.json")


@pytest.mark.asyncio
async def test_url_rejects_private_target_and_redirect_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEO_FETCH_ALLOWED_HOSTS", "vendor.example,127.0.0.1")
    for url in (
        "http://vendor.example/doc.json",
        "https://vendor.example:444/doc.json",
        "https://user:pass@vendor.example/doc.json",
        "https://vendor.example/doc.json?token=secret",
        "https://127.0.0.1/doc.json",
    ):
        with pytest.raises(fetcher.FetchError):
            await fetcher._check_url(url)

    async def private_dns(_host: str) -> None:
        raise fetcher.FetchError("URL host resolves to a non-public address")

    monkeypatch.setattr(fetcher, "_check_public_dns", private_dns)
    with pytest.raises(fetcher.FetchError, match="non-public"):
        await fetcher._check_url("https://vendor.example/doc.json")


@pytest.mark.asyncio
async def test_real_localhost_dns_is_not_public() -> None:
    with pytest.raises(fetcher.FetchError, match="non-public"):
        await fetcher._check_public_dns("localhost")


@pytest.mark.asyncio
async def test_dns_resolution_has_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowLoop:
        async def getaddrinfo(self, *_args: object, **_kwargs: object) -> list[object]:
            await asyncio.sleep(1)
            return []

    monkeypatch.setattr(fetcher, "get_running_loop", SlowLoop)
    monkeypatch.setattr(fetcher, "DEFAULT_TIMEOUT_S", 0.001)
    with pytest.raises(fetcher.FetchError, match="resolution timed out"):
        await fetcher._check_public_dns("vendor.example")


@pytest.mark.asyncio
async def test_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEO_FETCH_ALLOWED_HOSTS", "vendor.example")

    async def public_dns(_host: str) -> str:
        return "93.184.215.14"

    monkeypatch.setattr(fetcher, "_check_public_dns", public_dns)
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(fetcher.FetchError, match="redirects are not allowed"):
            await fetcher.fetch_and_parse(client, "https://vendor.example/doc.json")
    assert visited == ["https://93.184.215.14/doc.json"]


@pytest.mark.asyncio
async def test_streaming_response_stops_at_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEO_FETCH_ALLOWED_HOSTS", "vendor.example")

    async def public_dns(_host: str) -> str:
        return "93.184.215.14"

    monkeypatch.setattr(fetcher, "_check_public_dns", public_dns)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 100))
    ) as client:
        with pytest.raises(fetcher.FetchError, match="size limit"):
            await fetcher.fetch_and_parse(client, "https://vendor.example/doc.json", max_bytes=16)


@pytest.mark.parametrize("body", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"[]", b"not json"])
def test_invalid_or_ambiguous_json_rejected(body: bytes) -> None:
    with pytest.raises(fetcher.FetchError):
        fetcher.parse_document(body)
