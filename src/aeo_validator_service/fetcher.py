"""
URL fetcher with canonical content-hash.

Same hashing convention as procurement-decision-api (sha256 over canonical
JSON: sorted keys, no whitespace) so the two services produce identical
content_hash values for identical documents.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import socket
from asyncio import get_running_loop, wait_for
from datetime import UTC, datetime
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_URL_LENGTH = 2048


class FetchError(Exception):
    """Caller-facing fetch failure (HTTP error / parse failure / size cap / timeout)."""


def canonical_hash(parsed: object) -> str:
    """Legacy Suite sha256 profile: Python sorted-key JSON, not RFC 8785 JCS."""
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _allowed_hosts() -> set[str]:
    """Network fetches are opt-in and limited to exact hostnames."""
    return {
        host.strip().lower().rstrip(".")
        for host in os.getenv("AEO_FETCH_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }


async def _check_public_dns(host: str) -> str:
    try:
        answers = await wait_for(
            get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM),
            timeout=DEFAULT_TIMEOUT_S,
        )
    except TimeoutError as err:
        raise FetchError("URL host resolution timed out") from err
    except OSError as err:
        raise FetchError("URL host could not be resolved") from err
    if not answers:
        raise FetchError("URL host could not be resolved")
    addresses: list[str] = []
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise FetchError("URL host resolves to a non-public address")
        addresses.append(str(address))
    return addresses[0]


async def _check_url(url: str) -> tuple[httpx.URL, str, str]:
    if len(url) > MAX_URL_LENGTH:
        raise FetchError("URL is too long")
    try:
        parsed = httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as err:
        raise FetchError("invalid URL") from err
    host = (parsed.host or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.userinfo
        or parsed.fragment
        or parsed.query
    ):
        raise FetchError("URL must be HTTPS on port 443 without credentials, query, or fragment")
    if not host or host not in _allowed_hosts():
        raise FetchError("URL host is not allowed for fetching")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchError("IP-literal URLs are not allowed")
    address = await _check_public_dns(host)
    return parsed, host, address


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def parse_document(body_bytes: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            body_bytes.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, ValueError) as err:
        raise FetchError("invalid JSON document") from err
    if not isinstance(parsed, dict):
        raise FetchError("top-level JSON must be an object")
    return parsed


async def fetch_and_parse(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[dict[str, Any], str]:
    """
    Fetch `url`, enforce the size cap, parse as JSON, and return
    `(body, content_hash)`. Caller passes a shared `AsyncClient` so the
    service can reuse its client configuration. The actual request targets the
    checked IP, with the original hostname as the HTTP Host and TLS SNI.
    This binds the connection to the checked DNS answer.
    """
    parsed_url, host, address = await _check_url(url)
    # A request to the hostname would resolve it a second time inside httpx,
    # leaving a DNS rebinding window. Connect to the checked address instead.
    # sni_hostname is forwarded by httpx/httpcore to TLS certificate checking.
    pinned_url = parsed_url.copy_with(host=address)
    try:
        async with client.stream(
            "GET",
            pinned_url,
            headers={"Host": host, "Connection": "close"},
            extensions={"sni_hostname": host},
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise FetchError("redirects are not allowed")
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise FetchError("response exceeds size limit")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("response exceeds size limit")
                chunks.append(chunk)
    except httpx.TimeoutException as err:
        raise FetchError("fetch timed out") from err
    except httpx.HTTPStatusError as err:
        raise FetchError(f"upstream HTTP {err.response.status_code}") from err
    except httpx.RequestError as err:
        raise FetchError("fetch failed") from err
    parsed = parse_document(b"".join(chunks))
    return parsed, canonical_hash(parsed)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
