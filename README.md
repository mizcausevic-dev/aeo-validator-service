# aeo-validator-service

[![CI](https://github.com/mizcausevic-dev/aeo-validator-service/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/aeo-validator-service/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Local HTTP smoke validator for AEO and ten other recognised Kinetic Gain Suite document kinds.** It sniffs a `*_version` field, runs limited structural checks, computes a legacy JSON hash, and compares caller-triggered re-checks. The fourth layer of the AEO Reference Stack is a reference component, not a hosted trust decision service.

```
1. SDKs       aeo-sdk-python / -typescript / -rust / -go / -swift
2. CLI        aeo-cli
3. Crawler    aeo-crawler
4. Validator service   <- this repo
```

---

## Why an HTTP service instead of just the CLI

The CLI answers "does this document pass its checks right now?" This local HTTP reference adds three things for experimentation:

1. **HTTP for non-Python services.** The CLI is Python-only. The service is a curl away.
2. **Drift across manual checks.** Hash a vendor's AEO document, re-check it later, and identify changed top-level fields.
3. **Bounded watches.** Default watches are process-local. An opt-in single-node pilot mode stores tenant-scoped watches and an operational audit trail in SQLite. There is no scheduler or alert delivery.

---

## Install

```bash
pip install aeo-validator-service
aeo-validator-service          # binds 127.0.0.1:8091
```

Python 3.11+. Runtime deps: `fastapi`, `httpx`, `httpcore`, `pydantic`, `uvicorn`. Remote fetching is disabled until `AEO_FETCH_ALLOWED_HOSTS` names exact vendor hostnames. For example, set `AEO_FETCH_ALLOWED_HOSTS=acme.example` before starting the service. URLs must use HTTPS on port 443, cannot redirect, and cannot carry credentials or query parameters. DNS answers are checked for non-public addresses; the request connects to a checked IP while preserving the vendor hostname for HTTP Host and TLS SNI. The tested dependency bounds are `httpx>=0.28.1,<0.29` and `httpcore>=1.0.9,<1.1`. A deployment egress firewall is still required.

The default local mode is unauthenticated and binds loopback. `AEO_HOSTED_MODE=1` enables static tenant bearer credentials, per-tenant remote-host authorization, a 2 MiB plus wrapper-overhead request cap with a 10-second read deadline, and SQLite watch and audit retention. It fails startup when credentials or a durable path are missing, and refuses a remote `AUDIT_STREAM_URL`. See [HOSTED_PILOT.md](https://github.com/mizcausevic-dev/aeo-validator-service/blob/main/HOSTED_PILOT.md) for configuration and limits. This mode is for a **synthetic, single-node pilot**, not customer traffic: it has no buyer identity proof, roles, gateway rate limit, encrypted storage policy, or tested live rollback.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + supported spec list. |
| GET | `/healthz` | Liveness probe. |
| POST | `/validate/by-url` | Fetch + validate by URL. One-shot, no watch. |
| POST | `/validate/inline` | Validate an already-fetched document — no network. |
| POST | `/watches` | Create a bounded watch for a URL; the initial fetch + validation runs synchronously. |
| GET | `/watches` | List watch IDs. |
| GET | `/watches/{id}` | Watch metadata + last result. |
| GET | `/watches/{id}/history` | Up to 20 recent results (oldest → newest), without fetched bodies. |
| POST | `/watches/{id}/recheck` | Caller-triggered re-fetch + validate. Returns a structured **DriftReport** vs. the previous result. |
| DELETE | `/watches/{id}` | Delete the watch. |

---

## Supported specs

The validator sniffs the spec kind from the top-level `*_version` field — the same trick the [unified visualizer](https://github.com/mizcausevic-dev/kinetic-gain-visualizer) uses. Eleven specs auto-detected:

| Spec | Detected via |
| --- | --- |
| AEO Protocol | `aeo_version` |
| Prompt Provenance | `provenance_version` |
| Agent Cards | `agent_card_version` |
| AI Evidence Format | `evidence_version` |
| MCP Tool Cards | `tool_card_version` |
| AI Tutor Cards | `tutor_card_version` |
| Student AI Disclosure | `disclosure_version` |
| Classroom AI AUP | `aup_version` |
| Clinical AI Disclosure | `clinical_ai_card_version` |
| AI Incident Card | `incident_card_version` |
| AI Procurement Decision Card | `decision_card_version` |

For all eleven it checks for a nonblank version field. Additional structural smoke checks currently cover AEO, agent cards, tool cards, incident cards, and decision cards. The other six kinds receive the version check only. The service also rejects unknown or ambiguous kinds. A watch's optional `spec_hint` must match the detected kind.

These are examples of the limited checks:

- **Universal checks** — version field present, non-blank
- **Spec-specific smoke checks** — AEO entity has `id` + `type` + `name`; agent-card has `agent_id` + `capabilities`; decision-card with `approved-with-conditions` requires non-empty `conditions[]`; etc.

`valid: true` means only these smoke checks passed. It does not prove schema conformance, source identity, vendor claims, signature validity, buyer approval, or permission to act. Use the corresponding specification and a trusted signature/authority workflow when those assurances matter.

---

## Drift report

```json
{
  "url": "https://acme.example/.well-known/aeo.json",
  "drifted": true,
  "spec_changed": false,
  "became_invalid": false,
  "became_valid": false,
  "content_hash_before": "sha256:9a3f...",
  "content_hash_after":  "sha256:b7d1...",
  "added_fields":   ["claims"],
  "removed_fields": [],
  "changed_fields": ["entity"],
  "before_issues":  0,
  "after_issues":   0
}
```

A drift is *any* of: hash changed, spec kind changed, validity flipped, or top-level field set changed. Webhooks-on-drift are an obvious follow-up (PR welcome).

---

## Quick start

```bash
# One-shot validation:
# Start the service with AEO_FETCH_ALLOWED_HOSTS=acme.example first.
curl -X POST http://localhost:8091/validate/by-url \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://acme.example/.well-known/aeo.json", "include_body": true}'

# Process-local watch:
curl -X POST http://localhost:8091/watches \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://acme.example/.well-known/aeo.json"}'
# -> {"watch_id": "a1b2c3", ...}

# Trigger a re-check while the same process is still running:
curl -X POST http://localhost:8091/watches/a1b2c3/recheck
```

---

## Hashing convention

`content_hash` is `sha256:<hex>` over Python sorted-key, compact JSON, with default ASCII escaping. It matches the **legacy** hash in [`procurement-decision-api`](https://github.com/mizcausevic-dev/procurement-decision-api) for the same parsed JSON values. It is **not** the versioned RFC 8785 JCS hash used by `hash-attestation-rs` v0.2 or procurement's `document_hashes[]`. Do not compare these hash profiles or treat a hash as publisher authentication.

Fetched JSON rejects duplicate object keys and non-finite numbers. A fetch is capped at 2 MiB while streaming. Inline request bodies are capped at 2 MiB plus wrapper overhead before JSON parsing. The default local store holds at most 16 watches; hosted pilot storage holds at most 16 per tenant and 64 total, with 20 results per watch. Default watch data is lost on restart. Hosted pilot watches expire 1–30 days after creation, including when that setting is shortened on restart. Watch API responses omit document bodies, but the pilot SQLite file stores each watch's latest body for drift comparison. The optional remote audit-stream event is local-mode only and best effort. The pilot SQLite operational audit records tenant, action, timestamp, validity, and hash without document bodies.

---

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy src
pytest -v
```

Test fixtures use `httpx.MockTransport` so no vendor URL is fetched. The pinned-IP/TLS handoff is tested with the installed `httpcore` network backend, and the hosted pilot has local tenant, persistence, retention, and backup/restore tests. CI matrix Python 3.11 / 3.12 / 3.13. These local tests do not prove a deployed gateway, real buyer identity, storage encryption, external egress rules, or a live operator rollback.

---

## Related in this ecosystem

- **[aeo-protocol-spec](https://github.com/mizcausevic-dev/aeo-protocol-spec)** — the spec this service validates.
- **[aeo-cli](https://github.com/mizcausevic-dev/aeo-cli)** · **[aeo-crawler](https://github.com/mizcausevic-dev/aeo-crawler)** — layers 2 and 3 of the AEO Reference Stack.
- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** — its legacy hash matches this service's hash for the same parsed JSON values. Its versioned JCS hash for signed documents is a different profile.
- More at [kineticgain.com](https://kineticgain.com/).

---

## License

MIT. See [LICENSE](LICENSE).
