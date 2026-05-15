"""
Drift detection — diff two `ValidationResult` snapshots for the same URL.

Drift is reported as a small structured object so dashboards + Slack
webhooks can render "what changed" without re-fetching anything.
"""

from __future__ import annotations

from .models import DriftReport, ValidationResult


def compute_drift(before: ValidationResult | None, after: ValidationResult) -> DriftReport:
    """
    Diff two results. `before` may be `None` for the first observation of a URL.

    A drift is *any* of:
      - content_hash changed
      - spec kind changed
      - validity flipped in either direction
      - set of top-level field names changed
    """
    if before is None:
        return DriftReport(
            url=after.url,
            drifted=True,
            spec_changed=True,
            became_invalid=not after.valid,
            became_valid=after.valid,
            content_hash_before=None,
            content_hash_after=after.content_hash,
            added_fields=sorted((after.body or {}).keys()),
            removed_fields=[],
            changed_fields=[],
            before_issues=0,
            after_issues=len(after.issues),
        )

    before_body = before.body or {}
    after_body = after.body or {}
    before_keys = set(before_body.keys())
    after_keys = set(after_body.keys())

    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    # Shared keys whose values changed.
    changed = sorted(k for k in before_keys & after_keys if before_body.get(k) != after_body.get(k))

    return DriftReport(
        url=after.url,
        drifted=(
            before.content_hash != after.content_hash
            or before.spec != after.spec
            or before.valid != after.valid
            or added != []
            or removed != []
        ),
        spec_changed=before.spec != after.spec,
        became_invalid=before.valid and not after.valid,
        became_valid=not before.valid and after.valid,
        content_hash_before=before.content_hash,
        content_hash_after=after.content_hash,
        added_fields=added,
        removed_fields=removed,
        changed_fields=changed,
        before_issues=len(before.issues),
        after_issues=len(after.issues),
    )
