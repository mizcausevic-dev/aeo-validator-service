"""
Optional audit-stream-py integration.

When the `AUDIT_STREAM_URL` env var is set, this module fires
governance events at `{AUDIT_STREAM_URL}/events` for the moments the
service produces. Best-effort: a failed POST is logged, not raised —
audit-stream outages must never block watch creation, validation, or
drift detection.

Set `AUDIT_STREAM_URL=` (empty) or unset to disable. Set
`AUDIT_STREAM_TIMEOUT_S=2.5` to override the default fire-and-forget
timeout.

This uses the same opt-in event shape as `procurement-decision-api.audit_stream`.
It is best-effort and does not confirm durable acceptance.
"""

from __future__ import annotations

import math
import os
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 2.5


def is_enabled() -> bool:
    """True when a usable HTTP(S) audit destination is configured."""
    return base_url() is not None


def base_url() -> str | None:
    """HTTP(S) audit base URL without credentials, query, or fragment."""
    raw = os.environ.get("AUDIT_STREAM_URL", "").strip()
    if not raw:
        return None
    try:
        parsed = httpx.URL(raw)
    except (ValueError, httpx.InvalidURL):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        return None
    return str(parsed).rstrip("/")


def timeout_s() -> float:
    """Configured per-call timeout, bounded to 0.1-30 seconds."""
    raw = os.environ.get("AUDIT_STREAM_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(value):
        return DEFAULT_TIMEOUT_S
    return min(30.0, max(0.1, value))


async def emit(
    client: httpx.AsyncClient,
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """
    Fire one event. Silent no-op when AUDIT_STREAM_URL is unset.

    Kinds the validator service uses:
        watch_created            POST /watches returned a new watch
        watch_drifted            recheck reports drifted=True
        watch_validity_flipped   validity went True -> False or False -> True
    """
    url = base_url()
    if url is None:
        return

    body = {
        "kind": kind,
        "source": "aeo-validator-service",
        "payload": payload,
    }
    try:
        response = await client.post(
            f"{url}/events",
            json=body,
            timeout=timeout_s(),
        )
        response.raise_for_status()
    except (httpx.HTTPError, OSError):
        print("audit-stream emit failed", flush=True)
