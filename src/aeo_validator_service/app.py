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
  GET  /watches/{id}/history            up to 20 recent results, without bodies
  POST /watches/{id}/recheck            re-fetch + validate; returns the drift report
  DELETE /watches/{id}                  delete the watch
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import __version__, audit_stream
from .drift import compute_drift
from .fetcher import DEFAULT_TIMEOUT_S, FetchError, canonical_hash, fetch_and_parse, now_iso
from .models import DriftReport, SpecKind, ValidationIssue, ValidationResult, Watch
from .request_guard import RequestGuard
from .sqlite_watch_store import SQLiteWatchStore
from .tenant_auth import TenantAuth
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
    app.state.tenant_auth = TenantAuth.from_environment()
    app.state.validator = SuiteValidator()
    if app.state.tenant_auth.enabled:
        path = os.getenv("AEO_WATCH_DB_PATH", "")
        try:
            retention_days = int(os.getenv("AEO_WATCH_RETENTION_DAYS", "7"))
        except ValueError as err:
            raise ValueError("AEO_WATCH_RETENTION_DAYS must be an integer") from err
        app.state.watches = SQLiteWatchStore(path, retention_days=retention_days)
    else:
        app.state.watches = WatchStore()
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"User-Agent": f"aeo-validator-service/{__version__} (+https://kineticgain.com)"},
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        if isinstance(app.state.watches, SQLiteWatchStore):
            app.state.watches.close()


app = FastAPI(
    title="aeo-validator-service",
    version=__version__,
    description=(
        "Local HTTP smoke validator for eleven recognised Kinetic Gain Suite document kinds. "
        "URL fetching is opt-in; drift watches are bounded and caller-triggered."
    ),
    lifespan=_lifespan,
)
app.add_middleware(RequestGuard, service_app=app)


def _client() -> httpx.AsyncClient:
    # Use `cast` instead of `isinstance`: tests monkeypatch `httpx.AsyncClient`
    # to a factory function, which would make the isinstance check explode.
    return cast(httpx.AsyncClient, app.state.http_client)


def _validator() -> SuiteValidator:
    return cast(SuiteValidator, app.state.validator)


def _watches() -> WatchStore | SQLiteWatchStore:
    return cast(WatchStore | SQLiteWatchStore, app.state.watches)


def _tenant(request: Request) -> str:
    return cast(str, request.state.tenant)


def _audit_validation(tenant: str, action: str, result: ValidationResult) -> None:
    store = _watches()
    if isinstance(store, SQLiteWatchStore):
        store.audit_validation(tenant, action, result)


def _authorize_fetch(tenant: str, url: str) -> None:
    cast(TenantAuth, app.state.tenant_auth).authorize_fetch(tenant, url)


def _public_result(result: ValidationResult) -> ValidationResult:
    return result.model_copy(update={"body": None})


def _public_watch(watch: Watch) -> Watch:
    last = _public_result(watch.last_result) if watch.last_result else None
    return watch.model_copy(update={"last_result": last})


def _check_spec_hint(spec: SpecKind, hint: SpecKind | None, issues: list[ValidationIssue]) -> None:
    if hint is not None and hint != spec:
        issues.append(
            ValidationIssue(
                severity="error",
                kind="spec_hint_mismatch",
                message="detected spec does not match the watch's spec_hint",
            )
        )


@app.get("/", tags=["meta"])
async def root() -> dict[str, Any]:
    return {
        "name": "aeo-validator-service",
        "version": __version__,
        "description": (
            "Optionally fetches allowlisted Suite documents, runs limited structural checks, "
            "computes a legacy JSON hash, and compares caller-triggered re-checks."
        ),
        "validation_scope": "smoke checks only; no full schema, signature, or authority verification",
        "watch_storage": "single-node SQLite with retention"
        if _watches_is_durable()
        else "process-local memory",
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
            "POST /watches": "create a bounded watch for a URL",
            "GET  /watches": "list watch IDs",
            "GET  /watches/{id}": "watch metadata + last result",
            "GET  /watches/{id}/history": "up to 20 recent results, without bodies",
            "POST /watches/{id}/recheck": "re-fetch + validate; returns drift report",
            "DELETE /watches/{id}": "delete the watch",
        },
    }


def _watches_is_durable() -> bool:
    return isinstance(_watches(), SQLiteWatchStore)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate/by-url", tags=["validate"])
async def validate_by_url(req: _ValidateByUrlRequest, request: Request) -> ValidationResult:
    tenant = _tenant(request)
    _authorize_fetch(tenant, req.url)
    try:
        body, content_hash = await fetch_and_parse(_client(), req.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    result = SuiteValidator.result(
        url=req.url,
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=req.include_body,
    )
    _audit_validation(tenant, "validate_by_url", result)
    return result


@app.post("/validate/inline", tags=["validate"])
async def validate_inline(req: _ValidateInlineRequest, request: Request) -> ValidationResult:
    tenant = _tenant(request)
    body = req.body
    try:
        content_hash = canonical_hash(body)
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=400, detail="inline document is not valid JSON data") from err
    spec, version, issues = _validator().validate(body)
    result = SuiteValidator.result(
        url="inline://anonymous",
        body=body,
        fetched_at=now_iso(),
        content_hash=content_hash,
        spec=spec,
        spec_version=version,
        issues=issues,
        include_body=req.include_body,
    )
    _audit_validation(tenant, "validate_inline", result)
    return result


@app.post("/watches", tags=["watches"], status_code=201)
async def create_watch(req: _CreateWatchRequest, request: Request) -> Watch:
    tenant = _tenant(request)
    _authorize_fetch(tenant, req.url)
    if not _watches().has_capacity(tenant):
        raise HTTPException(status_code=429, detail="watch limit reached")
    try:
        body, content_hash = await fetch_and_parse(_client(), req.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    _check_spec_hint(spec, req.spec_hint, issues)
    # Retain the latest body internally for drift comparison. Public watch
    # responses omit it.
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
    store = _watches()
    try:
        if isinstance(store, SQLiteWatchStore):
            recorded = store.create_with_result(req.url, result, spec_hint=req.spec_hint, tenant=tenant)
        else:
            watch = store.create(req.url, spec_hint=req.spec_hint, tenant=tenant)
            recorded = store.record(watch.watch_id, result, tenant=tenant)
    except OverflowError as err:
        raise HTTPException(status_code=429, detail=str(err)) from err

    # Best-effort audit-stream emission.
    await audit_stream.emit(
        _client(),
        kind="watch_created",
        payload={
            "watch_id": recorded.watch_id,
            "url": req.url,
            "spec": spec,
            "spec_version": version,
            "content_hash": content_hash,
            "valid": result.valid,
        },
    )

    return _public_watch(recorded)


@app.get("/watches", tags=["watches"])
async def list_watches(request: Request) -> dict[str, list[str]]:
    return {"watch_ids": _watches().list_ids(tenant=_tenant(request))}


@app.get("/watches/{watch_id}", tags=["watches"])
async def get_watch(watch_id: str, request: Request) -> Watch:
    try:
        return _public_watch(_watches().get(watch_id, tenant=_tenant(request)))
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.get("/watches/{watch_id}/history", tags=["watches"])
async def get_watch_history(watch_id: str, request: Request) -> list[ValidationResult]:
    try:
        return [_public_result(result) for result in _watches().history(watch_id, tenant=_tenant(request))]
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.post("/watches/{watch_id}/recheck", tags=["watches"])
async def recheck_watch(watch_id: str, request: Request) -> DriftReport:
    tenant = _tenant(request)
    try:
        watch = _watches().get(watch_id, tenant=tenant)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    _authorize_fetch(tenant, watch.url)

    try:
        body, content_hash = await fetch_and_parse(_client(), watch.url)
    except FetchError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    spec, version, issues = _validator().validate(body)
    _check_spec_hint(spec, watch.spec_hint, issues)
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
    try:
        previous = _watches().previous(watch_id, tenant=tenant)
        _watches().record(watch_id, new_result, tenant=tenant)
    except KeyError as err:
        raise HTTPException(status_code=404, detail="unknown watch_id") from err
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
async def delete_watch(watch_id: str, request: Request) -> None:
    _watches().delete(watch_id, tenant=_tenant(request))
