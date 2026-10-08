"""Opt-in, static tenant bearer credentials for a single-node hosted pilot.

Credentials are configured as SHA-256 digests of independently generated,
high-entropy bearer tokens. Changing the configuration and restarting the
service revokes old tokens. This is not a buyer identity provider.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re

import httpx
from fastapi import HTTPException, Request

_TENANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}\Z")


class TenantAuth:
    def __init__(
        self,
        enabled: bool,
        digests: dict[str, tuple[str, ...]],
        allowed_hosts: dict[str, set[str]] | None = None,
    ) -> None:
        self.enabled = enabled
        self._digests = digests
        self._allowed_hosts = allowed_hosts or {}

    @classmethod
    def from_environment(cls) -> TenantAuth:
        mode = os.getenv("AEO_HOSTED_MODE", "0")
        if mode not in {"0", "1"}:
            raise ValueError("AEO_HOSTED_MODE must be 0 or 1")
        if mode == "0":
            return cls(False, {})
        raw = os.getenv("AEO_TENANT_TOKEN_SHA256", "")
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ValueError("AEO_TENANT_TOKEN_SHA256 must be a JSON tenant-to-digest map") from err
        if not isinstance(configured, dict) or not 1 <= len(configured) <= 100:
            raise ValueError("AEO_TENANT_TOKEN_SHA256 needs 1 to 100 tenants")
        digests: dict[str, tuple[str, ...]] = {}
        seen: set[str] = set()
        for tenant, tenant_digests in configured.items():
            if not isinstance(tenant, str) or not _TENANT_ID.fullmatch(tenant):
                raise ValueError("invalid tenant ID in AEO_TENANT_TOKEN_SHA256")
            if isinstance(tenant_digests, str):
                tenant_digests = [tenant_digests]
            if not isinstance(tenant_digests, list) or not 1 <= len(tenant_digests) <= 2:
                raise ValueError("each tenant needs one or two token digests")
            for digest in tenant_digests:
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    raise ValueError("invalid token digest in AEO_TENANT_TOKEN_SHA256")
                if digest in seen:
                    raise ValueError("token digest cannot authorize multiple tenants")
                seen.add(digest)
            digests[tenant] = tuple(tenant_digests)
        try:
            configured_hosts = json.loads(os.getenv("AEO_TENANT_ALLOWED_HOSTS", "{}"))
        except json.JSONDecodeError as err:
            raise ValueError("AEO_TENANT_ALLOWED_HOSTS must be a JSON tenant-to-hosts map") from err
        if not isinstance(configured_hosts, dict):
            raise ValueError("AEO_TENANT_ALLOWED_HOSTS must be a JSON tenant-to-hosts map")
        allowed_hosts: dict[str, set[str]] = {}
        for tenant, hosts in configured_hosts.items():
            if tenant not in digests or not isinstance(hosts, list) or len(hosts) > 20:
                raise ValueError("invalid tenant host allowlist")
            normalized: set[str] = set()
            for host in hosts:
                if not isinstance(host, str) or not _HOST.fullmatch(host) or ".." in host:
                    raise ValueError("invalid tenant host allowlist")
                normalized.add(host)
            allowed_hosts[tenant] = normalized
        return cls(True, digests, allowed_hosts)

    def tenant(self, request: Request) -> str:
        if not self.enabled:
            return "local"
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not 32 <= len(token) <= 512:
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for tenant, expected_digests in self._digests.items():
            for expected in expected_digests:
                if hmac.compare_digest(digest, expected):
                    return tenant
        raise HTTPException(401, "invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    def authorize_fetch(self, tenant: str, url: str) -> None:
        if not self.enabled:
            return
        try:
            host = httpx.URL(url).host
        except (ValueError, httpx.InvalidURL):
            host = None
        if not host or host not in self._allowed_hosts.get(tenant, set()):
            raise HTTPException(403, "URL host not authorized for tenant")
