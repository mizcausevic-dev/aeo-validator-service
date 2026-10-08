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
3. **Process-local watches.** A watch holds a bounded history for comparison while this process runs. There is no scheduler, durable store, or alert delivery.

---

## Install

```bash
pip install aeo-validator-service
aeo-validator-service          # binds 127.0.0.1:8091
```

Python 3.11+. Runtime deps: `fastapi`, `httpx`, `pydantic`, `uvicorn`. Remote fetching is disabled until `AEO_FETCH_ALLOWED_HOSTS` names exact vendor hostnames. For example, set `AEO_FETCH_ALLOWED_HOSTS=acme.example` before starting the service. URLs must use HTTPS on port 443, cannot redirect, and cannot carry credentials or query parameters. DNS is checked for non-public addresses before each fetch; a network egress policy is still needed to close DNS rebinding at the actual connection.

All endpoints are unauthenticated. Keep the process on loopback for local development. Do not expose it on a public network or use it for buyer authorization. A hosted service also needs authenticated callers, tenant isolation, network egress enforcement, rate limits, durable watch and audit state, and an operator runbook.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + supported spec list. |
| GET | `/healthz` | Liveness probe. |
| POST | `/validate/by-url` | Fetch + validate by URL. One-shot, no watch. |
| POST | `/validate/inline` | Validate an already-fetched document — no network. |
| POST | `/watches` | Create a process-local watch for a URL; the initial fetch + validation runs synchronously. |
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

Fetched JSON rejects duplicate object keys and non-finite numbers. A fetch is capped at 2 MiB while streaming. The 16-watch limit and 20-result history bound limit local memory use; all watch data is lost on restart. Watch API responses omit document bodies. The optional audit-stream event contains the URL and document hash, and its delivery is best effort.

---

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy src
pytest -v
```

Test fixtures use `httpx.MockTransport` so no vendor URL is fetched. CI matrix Python 3.11 / 3.12 / 3.13. Local tests do not prove hosted egress, authentication, persistence, or an operator rollback.

---

## Related in this ecosystem

- **[aeo-protocol-spec](https://github.com/mizcausevic-dev/aeo-protocol-spec)** — the spec this service validates.
- **[aeo-cli](https://github.com/mizcausevic-dev/aeo-cli)** · **[aeo-crawler](https://github.com/mizcausevic-dev/aeo-crawler)** — layers 2 and 3 of the AEO Reference Stack.
- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** — its legacy hash matches this service's hash for the same parsed JSON values. Its versioned JCS hash for signed documents is a different profile.
- More at [kineticgain.com](https://kineticgain.com/).

---

## License

MIT. See [LICENSE](LICENSE).
