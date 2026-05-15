# aeo-validator-service

[![CI](https://github.com/mizcausevic-dev/aeo-validator-service/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/aeo-validator-service/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Always-on validator service for AEO and the rest of the Kinetic Gain Protocol Suite.** Fetches a vendor URL, validates the document against the right spec (sniffed from `*_version`), hashes it canonically, and tracks **drift** across re-checks. The fourth layer of the AEO Reference Stack — what the CLI is, but always running, with history.

```
1. SDKs       aeo-sdk-python / -typescript / -rust / -go / -swift
2. CLI        aeo-cli
3. Crawler    aeo-crawler
4. Validator service   <- this repo
```

---

## Why a service instead of just the CLI

The CLI answers "is this doc valid right now." That's enough on a developer laptop. In production you want three more things:

1. **HTTP for non-Python services.** The CLI is Python-only. The service is a curl away.
2. **Drift over time.** Hash a vendor's AEO doc today, hash it again tomorrow, and tell me what changed. Not just "different" — *which field* changed. That's the signal that something's worth a Slack ping.
3. **Watches.** "Check this URL every hour and let me know when it goes invalid or its spec changes." The service holds the history so the diff has somewhere to anchor.

---

## Install

```bash
pip install aeo-validator-service
aeo-validator-service          # binds 0.0.0.0:8091
```

Python 3.11+. Runtime deps: `fastapi`, `httpx`, `pydantic`, `uvicorn`.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + supported spec list. |
| GET | `/healthz` | Liveness probe. |
| POST | `/validate/by-url` | Fetch + validate by URL. One-shot, no watch. |
| POST | `/validate/inline` | Validate an already-fetched document — no network. |
| POST | `/watches` | Create a persistent watch for a URL; the initial fetch + validation runs synchronously. |
| GET | `/watches` | List watch IDs. |
| GET | `/watches/{id}` | Watch metadata + last result. |
| GET | `/watches/{id}/history` | Full validation history (oldest → newest). |
| POST | `/watches/{id}/recheck` | Re-fetch + validate. Returns a structured **DriftReport** vs. the previous result. |
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

For each one the validator runs:

- **Universal checks** — version field present, non-blank
- **Spec-specific smoke checks** — AEO entity has `id` + `type` + `name`; agent-card has `agent_id` + `capabilities`; decision-card with `approved-with-conditions` requires non-empty `conditions[]`; etc.

This isn't a full Schema validator — punt to the SDKs when every-field-typed validation is needed. The point of *this* layer is "does it look right at a glance" plus drift tracking.

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
curl -X POST http://localhost:8091/validate/by-url \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://acme.example/.well-known/aeo.json", "include_body": true}'

# Persistent watch:
curl -X POST http://localhost:8091/watches \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://acme.example/.well-known/aeo.json"}'
# -> {"watch_id": "a1b2c3", ...}

# Some time later — re-check and see what changed:
curl -X POST http://localhost:8091/watches/a1b2c3/recheck
```

---

## Hashing convention

`content_hash` is `sha256:<hex>` over canonical JSON — sorted keys, no whitespace, UTF-8. Same convention as [`procurement-decision-api`](https://github.com/mizcausevic-dev/procurement-decision-api), so the two services produce **identical** `content_hash` values for identical documents.

---

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy src
pytest -v
```

Test fixtures use `httpx.MockTransport` so nothing touches the network. CI matrix Python 3.11 / 3.12 / 3.13.

---

## Related in this ecosystem

- **[aeo-protocol-spec](https://github.com/mizcausevic-dev/aeo-protocol-spec)** — the spec this service validates.
- **[aeo-cli](https://github.com/mizcausevic-dev/aeo-cli)** · **[aeo-crawler](https://github.com/mizcausevic-dev/aeo-crawler)** — layers 2 and 3 of the AEO Reference Stack.
- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** — uses the same canonical-hash convention; the two pair naturally.
- More at [kineticgain.com](https://kineticgain.com/).

---

## License

MIT. See [LICENSE](LICENSE).
