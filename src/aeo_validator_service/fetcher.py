"""
URL fetcher with canonical content-hash.

Same hashing convention as procurement-decision-api (sha256 over canonical
JSON: sorted keys, no whitespace) so the two services produce identical
content_hash values for identical documents.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


class FetchError(Exception):
    """Caller-facing fetch failure (HTTP error / parse failure / size cap / timeout)."""


def canonical_hash(parsed: object) -> str:
    """sha256 of canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


async def fetch_and_parse(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[dict[str, Any], str]:
    """
    Fetch `url`, enforce the size cap, parse as JSON, and return
    `(body, content_hash)`. Caller passes a shared `AsyncClient` so the
    service can reuse the connection pool across requests.
    """
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException as err:
        raise FetchError(f"{url}: timeout") from err
    except httpx.HTTPStatusError as err:
        raise FetchError(f"{url}: HTTP {err.response.status_code}") from err
    except httpx.RequestError as err:
        raise FetchError(f"{url}: {type(err).__name__}: {err}") from err

    if response.headers.get("content-length"):
        try:
            if int(response.headers["content-length"]) > max_bytes:
                raise FetchError(f"{url}: content-length exceeds {max_bytes} bytes")
        except ValueError:
            pass

    body_bytes = response.content
    if len(body_bytes) > max_bytes:
        raise FetchError(f"{url}: response body exceeds {max_bytes} bytes")

    try:
        parsed = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise FetchError(f"{url}: invalid JSON ({err})") from err

    if not isinstance(parsed, dict):
        raise FetchError(f"{url}: top-level JSON must be an object")

    return parsed, canonical_hash(parsed)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
