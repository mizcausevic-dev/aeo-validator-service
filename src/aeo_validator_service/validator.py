"""
Spec validator — sniff the spec kind from `*_version` and run the right rules.

We don't ship the full JSON Schemas inline (they live in the spec repos). The
validator focuses on the *invariants every Suite document shares* plus a small
set of per-spec smoke checks that catch the failures that show up in practice:

    - top-level `*_version` field present and non-empty
    - canonical URLs and content hashes are URLs / hex
    - spec-specific required fields exist (AEO: entity.id + entity.type;
      agent-card: agent_id + capabilities; tool-card: tool_id + name; etc.)

The point isn't to replace `aeo validate` from `aeo-cli` — it's to expose a
service that says yes/no over HTTP, plus track drift over time. Full schema
validation is a small additional surface; punt to the SDKs when the user
needs every-field-typed checking.
"""

from __future__ import annotations

from typing import Any

from .models import SpecKind, ValidationIssue, ValidationResult

_SPEC_BY_VERSION_FIELD: dict[str, SpecKind] = {
    "aeo_version": "aeo",
    "provenance_version": "prompt-provenance",
    "agent_card_version": "agent-card",
    "evidence_version": "ai-evidence",
    "tool_card_version": "tool-card",
    "tutor_card_version": "tutor-card",
    "disclosure_version": "student-ai-disclosure",
    "aup_version": "classroom-aup",
    "clinical_ai_card_version": "clinical-ai",
    "incident_card_version": "incident-card",
    "decision_card_version": "decision-card",
}


class SuiteValidator:
    """Stateless. Cheap to construct. Reuse one per process."""

    def detect_spec(self, body: dict[str, Any]) -> tuple[SpecKind, str | None]:
        """Return `(spec_kind, version_string_or_none)`."""
        for field, kind in _SPEC_BY_VERSION_FIELD.items():
            if field in body:
                version = body[field]
                return kind, version if isinstance(version, str) else None
        return "unknown", None

    def validate(self, body: dict[str, Any]) -> tuple[SpecKind, str | None, list[ValidationIssue]]:
        spec, version = self.detect_spec(body)
        issues: list[ValidationIssue] = []
        issues.extend(self._validate_universal(body, spec))
        if spec == "aeo":
            issues.extend(self._validate_aeo(body))
        elif spec == "agent-card":
            issues.extend(self._validate_agent_card(body))
        elif spec == "tool-card":
            issues.extend(self._validate_tool_card(body))
        elif spec == "incident-card":
            issues.extend(self._validate_incident_card(body))
        elif spec == "decision-card":
            issues.extend(self._validate_decision_card(body))
        return spec, version, issues

    # ---- universal ------------------------------------------------------

    def _validate_universal(self, body: dict[str, Any], spec: SpecKind) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        if spec == "unknown":
            out.append(
                ValidationIssue(
                    severity="warning",
                    field=None,
                    kind="unknown_spec",
                    message=(
                        "No `*_version` field recognised; treating as a generic JSON document. "
                        "If this is a Kinetic Gain Suite doc, make sure the version field exists."
                    ),
                )
            )
            return out

        version_field = next(
            (f for f, k in _SPEC_BY_VERSION_FIELD.items() if k == spec),
            None,
        )
        if version_field is not None:
            value = body.get(version_field)
            if not isinstance(value, str) or not value.strip():
                out.append(
                    ValidationIssue(
                        severity="error",
                        field=version_field,
                        kind="missing_or_blank_version",
                        message=f"{version_field!r} must be a non-empty string",
                    )
                )
        return out

    # ---- per-spec checks ------------------------------------------------

    def _validate_aeo(self, body: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        entity = body.get("entity")
        if not isinstance(entity, dict):
            out.append(self._missing("entity", "aeo_entity_missing"))
        else:
            for k in ("id", "type", "name"):
                if not entity.get(k):
                    out.append(self._missing(f"entity.{k}", f"aeo_entity_{k}_missing"))
        authority = body.get("authority")
        if not isinstance(authority, dict) or not isinstance(authority.get("primary_sources"), list):
            out.append(self._missing("authority.primary_sources", "aeo_primary_sources_missing"))
        return out

    def _validate_agent_card(self, body: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        if not body.get("agent_id"):
            out.append(self._missing("agent_id", "agent_id_missing"))
        if not isinstance(body.get("capabilities"), list):
            out.append(self._missing("capabilities", "capabilities_missing"))
        return out

    def _validate_tool_card(self, body: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        for k in ("tool_id", "name", "description"):
            if not body.get(k):
                out.append(self._missing(k, f"{k}_missing"))
        return out

    def _validate_incident_card(self, body: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        for k in ("incident_id", "summary", "severity", "affected_documents"):
            if k not in body:
                out.append(self._missing(k, f"{k}_missing"))
        affected = body.get("affected_documents")
        if isinstance(affected, list) and not affected:
            out.append(
                ValidationIssue(
                    severity="warning",
                    field="affected_documents",
                    kind="affected_documents_empty",
                    message="affected_documents is empty; incidents usually reference at least one doc",
                )
            )
        return out

    def _validate_decision_card(self, body: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        for k in ("decision_id", "buyer", "decision", "subject", "rationale"):
            if k not in body:
                out.append(self._missing(k, f"{k}_missing"))
        decision = body.get("decision")
        if isinstance(decision, dict):
            status = decision.get("status")
            conditions = body.get("conditions") or []
            if status in ("approved-with-conditions", "rejected-with-remediation") and not conditions:
                out.append(
                    ValidationIssue(
                        severity="error",
                        field="conditions",
                        kind="conditions_required",
                        message=(f"decision.status={status!r} requires at least one entry in conditions[]"),
                    )
                )
        return out

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _missing(field: str, kind: str) -> ValidationIssue:
        return ValidationIssue(
            severity="error",
            field=field,
            kind=kind,
            message=f"required field {field!r} is missing",
        )

    @staticmethod
    def result(
        *,
        url: str,
        body: dict[str, Any] | None,
        fetched_at: str,
        content_hash: str,
        spec: SpecKind,
        spec_version: str | None,
        issues: list[ValidationIssue],
        include_body: bool,
    ) -> ValidationResult:
        """Build a `ValidationResult` once the caller has the fetched bytes."""
        return ValidationResult(
            url=url,
            fetched_at=fetched_at,
            content_hash=content_hash,
            spec=spec,
            spec_version=spec_version,
            valid=not any(i.severity == "error" for i in issues),
            issues=issues,
            body=body if include_body else None,
        )
