# Hosted pilot boundary

This source includes an **opt-in, single-process pilot mode** for synthetic or public Suite documents. No deployment target is configured in this repository. It is not a customer identity, authorization, or production operations system.

## Configuration

Keep `HOST=127.0.0.1` unless a named deployment places a TLS gateway and network policy in front of the process. Configure these environment variables in that environment, not in Git:

| Variable | Pilot behavior |
| --- | --- |
| `AEO_HOSTED_MODE=1` | Enables the tenant guard and SQLite store. Missing credentials or database path fails startup. |
| `AEO_TENANT_TOKEN_SHA256` | JSON map from tenant ID to one or two lowercase SHA-256 hex digests of independently generated, high-entropy bearer tokens. Example shape: `{"buyer-a":["<64 hex characters>"]}`. Do not use the example text as a credential. |
| `AEO_WATCH_DB_PATH` | Absolute path to a SQLite file in an existing, access-restricted directory on persistent storage. |
| `AEO_WATCH_RETENTION_DAYS` | 1–30 days, default 7. Each watch has a fixed expiry from creation; rechecks do not extend it. |
| `AEO_FETCH_ALLOWED_HOSTS` | Global exact-host fetch allowlist. Unset means no remote fetch. |
| `AEO_TENANT_ALLOWED_HOSTS` | JSON map from tenant ID to exact hosts that tenant may fetch. Unset means no tenant may fetch. A host must appear in both allowlists. |

Generate token values outside the repository in a credential manager. Give each tenant a different token. Pass `Authorization: Bearer <token>` on all routes except `/` and `/healthz`. The service stores only configured token digests in memory, compares digests in constant time, and does not emit token values in the SQLite audit trail. A global token does not grant a tenant access to another tenant's watches. Invalid and cross-tenant watch IDs return 404.

For rotation, temporarily list two digests for the same tenant, restart the single process, move the caller to the new token, remove the old digest, and restart again. Removing a digest and restarting revokes it. There is no live key-management API or buyer identity verification. Protect the config source and restart path as credentials.

## Data and egress boundary

- The pilot SQLite file holds watch metadata, the latest fetched document body for top-level drift comparison, up to 20 results per watch, and operational audit rows. Older result bodies are dropped when a new result is recorded. One-shot validation responses are not stored, but their tenant, action, validity, and content hash are audited. Watch create, recheck, and delete are audited without bearer tokens or fetched bodies.
- At startup and on each watch or audit access, records past the configured retention are removed. There is no independent timed sweeper. SQLite deletion does not prove secure erasure from WAL pages, filesystem snapshots, or backups. Define storage encryption, backup expiry, and deletion verification for a real target before accepting customer data.
- Outbound vendor fetches require both allowlists, HTTPS port 443, no URL credentials/query/fragment, all-public DNS answers, no redirect, a streaming 2 MiB response cap, and a connection to a checked IP with the original hostname used for HTTP Host and TLS SNI. The service ignores proxy environment variables and disables keepalive reuse. The code path is bounded to `httpx>=0.28.1,<0.29` and `httpcore>=1.0.9,<1.1`.
- This still needs a deployment egress firewall, an ingress rate limit, TLS termination, logging controls, volume encryption, backup monitoring, and an operator incident path. Keep remote fetching disabled in a first synthetic pilot.

## Local rollback exercise

`pytest -q tests/test_hosted.py::test_local_sqlite_backup_and_restore_drill` creates a temporary SQLite store, takes a SQLite online backup, deletes a watch, opens the backup, and checks that the watch and its result are recoverable. It does not exercise a live operator, gateway, container/image rollback, or customer traffic.

For a named pilot target, the operator runbook must record the deployed artifact digest, deployment revision, database path, retention setting, backup location, and smoke request. Before an upgrade, take a SQLite online backup and verify it opens. A rollback then stops the process, restores the prior artifact and matching database snapshot, restarts it, and verifies health, tenant denial, watch visibility, and remote fetch disabled. Drill this on the actual target and record timestamps and evidence before claiming a hosted release.

## Remaining release gates

The static bearer mapping is a pilot control, not signed buyer identity or roles. A real customer service needs a named target, identity provider or verified signed buyer context, tenant-aware authorization for every data source, managed secret rotation and revocation, volume encryption and backup retention, gateway abuse controls, monitored egress policy, an operational rollback drill, and privacy review for the actual data and retention terms. Do not point this mode at customer traffic until those controls are implemented and verified at the deployed boundary.
