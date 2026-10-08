"""Authenticate before JSON parsing and cap inbound bodies before buffering."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 2 * 1024 * 1024 + 16 * 1024
MAX_REQUEST_READ_SECONDS = 10.0


class RequestGuard:
    def __init__(self, app: ASGIApp, *, service_app: FastAPI) -> None:
        self.app = app
        self.service_app = service_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        auth = self.service_app.state.tenant_auth
        if scope["path"] not in {"/", "/healthz"}:
            try:
                tenant = auth.tenant(Request(scope, receive))
            except HTTPException as err:
                await JSONResponse({"detail": err.detail}, status_code=err.status_code, headers=err.headers)(
                    scope, receive, send
                )
                return
            scope.setdefault("state", {})["tenant"] = tenant

        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_REQUEST_BYTES:
                    await JSONResponse({"detail": "request body exceeds size limit"}, status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await JSONResponse({"detail": "invalid content length"}, status_code=400)(
                    scope, receive, send
                )
                return

        chunks: list[bytes] = []
        total = 0
        too_large = False
        try:
            async with asyncio.timeout(MAX_REQUEST_READ_SECONDS):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    total += len(chunk)
                    if total > MAX_REQUEST_BYTES:
                        too_large = True
                        break
                    chunks.append(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "request body read timed out"}, status_code=408)(
                scope, receive, send
            )
            return
        if too_large:
            await JSONResponse({"detail": "request body exceeds size limit"}, status_code=413)(
                scope, receive, send
            )
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
