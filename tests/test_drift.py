"""Drift detector tests."""

from __future__ import annotations

from aeo_validator_service.drift import compute_drift
from aeo_validator_service.models import ValidationResult


def _r(
    *,
    hash_: str = "sha256:aaa",
    spec: str = "aeo",
    valid: bool = True,
    body: dict[str, object] | None = None,
    issues: int = 0,
) -> ValidationResult:
    issue_list = [{"severity": "error", "field": None, "kind": "k", "message": "m"} for _ in range(issues)]
    return ValidationResult.model_validate(
        {
            "url": "https://x/.well-known/aeo.json",
            "fetched_at": "2026-05-14T00:00:00+00:00",
            "content_hash": hash_,
            "spec": spec,
            "spec_version": "0.1",
            "valid": valid,
            "issues": issue_list,
            "body": body or {"aeo_version": "0.1"},
        }
    )


class TestDrift:
    def test_first_observation_drifts_with_added_fields(self) -> None:
        after = _r(body={"aeo_version": "0.1", "entity": {"id": "https://x/"}})
        d = compute_drift(None, after)
        assert d.drifted
        assert d.spec_changed
        assert "entity" in d.added_fields

    def test_no_change_no_drift(self) -> None:
        a = _r(body={"aeo_version": "0.1", "entity": "x"})
        b = _r(body={"aeo_version": "0.1", "entity": "x"})
        d = compute_drift(a, b)
        assert not d.drifted

    def test_hash_change_drifts(self) -> None:
        a = _r(hash_="sha256:aaa")
        b = _r(hash_="sha256:bbb", body={"aeo_version": "0.1"})
        d = compute_drift(a, b)
        assert d.drifted

    def test_validity_flip(self) -> None:
        a = _r(valid=True)
        b = _r(valid=False, hash_="sha256:bbb")
        d = compute_drift(a, b)
        assert d.became_invalid is True
        assert d.became_valid is False

    def test_field_diff(self) -> None:
        a = _r(body={"aeo_version": "0.1", "entity": "x"})
        b = _r(
            body={"aeo_version": "0.1", "entity": "y", "claims": []},
            hash_="sha256:bbb",
        )
        d = compute_drift(a, b)
        assert d.added_fields == ["claims"]
        assert d.changed_fields == ["entity"]
        assert d.removed_fields == []

    def test_spec_change_flagged(self) -> None:
        a = _r(spec="aeo")
        b = _r(spec="agent-card", hash_="sha256:bbb")
        d = compute_drift(a, b)
        assert d.spec_changed
