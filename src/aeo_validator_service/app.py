"""
FastAPI app — fetch + validate + drift-track.

Endpoints:

  GET  /                                service info
  GET  /healthz                         liveness probe

  POST /validate/by-url                 one-shot: fetch, validate, return result (no watch)
  POST /validate/inline                 validate an already-fetched document (no fetch)

  POST /watches                         { url } -> creates a watch, validates immediately
  GET  /watches                         list watch IDs
  GET  /watches/{id}                    fetch watch metadata + last result
  GET  /watches/{id}/history            full validation history
  POST /watches/{id}/recheck            re-fetch + validate; returns the drift report
  DELETE /watches/{id}                  delete the watch
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import __version__, audit_stream
from .drift import compute_drift
from .fetcher import DEFAULT_TIMEOUT_S, FetchError, canonical_hash, fetch_and_parse, now_iso
from .models import DriftReport, SpecKind, ValidationResult, Watch
from .validator import SuiteValidator
from .watch_store import WatchStore


class _ValidateByUrlRequest(BaseModel):
    url: str
    include_body: bool = False


class _ValidateInlineRequest(BaseModel):
    body: dict[str, Any]
    include_body: bool = False


class _CreateWatchRequest(BaseModel):
    url: str
    spec_hint: SpecKind | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
        follow_redirects=True,
        headers={"User-Agent": f"aeo-validator-service/{__version__} (+https://kineticgain.com)"},
    )
    app.state.validator = SuiteValidator()
    app.state.watches = WatchStore()
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="aeo-validator-service",
    version=__version__,
    description=(
        "Always-on validator for AEO + Kinetic Gain Protocol Suite documents. "
        "Layer 4 of the AEO Reference Stack."
    ),
    lifespan=_lifespan,
)


def _client() -> httpx.AsyncClient:
    # Use `cast` instead of `isinstance`: tests monkeypatch `httpx.AsyncClient`
    # to a factory function, which would make the isinstance check explode.
    return cast(httpx.AsyncClient, app.state.http_client)


def _validator() -> SuiteValidator:
    return cast(SuiteValidator, app.state.validator)


def _watches() -> WatchStore:
    return cast(WatchStore, app.state.watches)


@app.get("/", tags=["meta"])
async def root() -> dict[str, Any]:
    return {
        "name": "aeo-validator-service",
        "version": __version__,
        "description": (
            "Fetches Kinetic Gain Protocol Suite documents by URL, validates them, "
            "hashes them canonically, tracks drift across re-checks."
        ),
        "specs_supported": [
            "aeo",
            "agent-card",
            "prompt-provenance",
            "ai-evidence",
            "tool-card",
            "tutor-card",
            "student-ai-disclosure",
            "classroom-aup",
            "clinical-ai",
            "incident-card",
            "decision-card",
        ],
        "endpoints": {
            "GET  /": "this page",
            "GET  /healthz": "liveness probe",
            "POST /validate/by-url": "fetch + validate by URL (one-shot)",
            "POST /validate/inline": "validate an already-fetched document",
            "POST /watches": "create a persistent watch for a URL",
            "GET  /watches": "list watch IDs",
            "GET  /watches/{id}": "watch metadata + last result",
            "GET  /watches/{id}/history": "full validation history",
            "POST /watches/{id}/recheck": "re-fetch + validate; returns drift report",
            "DELETE /watches/{id}": "delete the watch",
        },
    }


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate/by-url", tags=["validate"])
async def validate_by_url(req: _ValidateByUrlRequest) -> ValidationResult:
    try:
        body, content_hash = await fetch_and_parse(_client(), req.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    return SuiteValidator.result(
        url=req.url,
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=req.include_body,
    )


@app.post("/validate/inline", tags=["validate"])
async def validate_inline(req: _ValidateInlineRequest) -> ValidationResult:
    body = req.body
    content_hash = canonical_hash(body)
    spec, version, issues = _validator().validate(body)
    return SuiteValidator.result(
        url="inline://anonymous",
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=req.include_body,
    )


@app.post("/watches", tags=["watches"], status_code=201)
async def create_watch(req: _CreateWatchRequest) -> Watch:
    try:
        body, content_hash = await fetch_and_parse(_client(), req.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    # Always store with the body so drift comparisons on subsequent rechecks
    # have a real baseline. API consumers can ignore the field.
    result = SuiteValidator.result(
        url=req.url,
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=True,
    )
    watch = _watches().create(req.url, spec_hint=req.spec_hint)
    recorded = _watches().record(watch.watch_id, result)

    # Best-effort audit-stream emission.
    await audit_stream.emit(
        _client(),
        kind="watch_created",
        payload={
            "watch_id": watch.watch_id,
            "url": req.url,
            "spec": spec,
            "spec_version": version,
            "content_hash": content_hash,
            "valid": result.valid,
        },
    )

    return recorded


@app.get("/watches", tags=["watches"])
async def list_watches() -> dict[str, list[str]]:
    return {"watch_ids": _watches().list_ids()}


@app.get("/watches/{watch_id}", tags=["watches"])
async def get_watch(watch_id: str) -> Watch:
    try:
        return _watches().get(watch_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.get("/watches/{watch_id}/history", tags=["watches"])
async def get_watch_history(watch_id: str) -> list[ValidationResult]:
    try:
        return _watches().history(watch_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.post("/watches/{watch_id}/recheck", tags=["watches"])
async def recheck_watch(watch_id: str) -> DriftReport:
    try:
        watch = _watches().get(watch_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    try:
        body, content_hash = await fetch_and_parse(_client(), watch.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    new_result = SuiteValidator.result(
        url=watch.url,
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=True,
    )
    previous = _watches().previous(watch_id)
    _watches().record(watch_id, new_result)
    drift = compute_drift(previous, new_result)

    # Best-effort audit-stream emission. We fire at most ONE event per
    # recheck — validity_flipped takes precedence over drifted since it's
    # the more actionable signal.
    if drift.became_invalid or drift.became_valid:
        await audit_stream.emit(
            _client(),
            kind="watch_validity_flipped",
            payload={
                "watch_id": watch_id,
                "url": watch.url,
                "became_invalid": drift.became_invalid,
                "became_valid": drift.became_valid,
                "spec": spec,
                "content_hash_before": drift.content_hash_before,
                "content_hash_after": drift.content_hash_after,
                "after_issues": drift.after_issues,
            },
        )
    elif drift.drifted:
        await audit_stream.emit(
            _client(),
            kind="watch_drifted",
            payload={
                "watch_id": watch_id,
                "url": watch.url,
                "spec": spec,
                "spec_changed": drift.spec_changed,
                "content_hash_before": drift.content_hash_before,
                "content_hash_after": drift.content_hash_after,
                "added_fields": drift.added_fields,
                "removed_fields": drift.removed_fields,
                "changed_fields": drift.changed_fields,
            },
        )

    return drift


@app.delete("/watches/{watch_id}", tags=["watches"], status_code=204)
async def delete_watch(watch_id: str) -> None:
    _watches().delete(watch_id)
