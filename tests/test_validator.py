"""Unit tests for SuiteValidator."""

from __future__ import annotations

from typing import Any

from aeo_validator_service.validator import SuiteValidator


class TestDetectSpec:
    def test_detects_aeo(self) -> None:
        spec, version = SuiteValidator().detect_spec({"aeo_version": "0.1"})
        assert spec == "aeo"
        assert version == "0.1"

    def test_detects_decision_card(self) -> None:
        spec, _ = SuiteValidator().detect_spec({"decision_card_version": "0.1"})
        assert spec == "decision-card"

    def test_unknown_for_no_version_field(self) -> None:
        spec, version = SuiteValidator().detect_spec({"foo": "bar"})
        assert spec == "unknown"
        assert version is None


class TestUniversalChecks:
    def test_blank_version_is_error(self) -> None:
        _, _, issues = SuiteValidator().validate({"aeo_version": ""})
        assert any(i.kind == "missing_or_blank_version" for i in issues)

    def test_unknown_spec_warning(self) -> None:
        _, _, issues = SuiteValidator().validate({"foo": "bar"})
        assert any(i.kind == "unknown_spec" and i.severity == "warning" for i in issues)


class TestAeoChecks:
    def _aeo(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "aeo_version": "0.1",
            "entity": {"id": "https://x/", "type": "Organization", "name": "Acme"},
            "authority": {"primary_sources": ["https://x/"]},
            "claims": [],
        }
        body.update(overrides)
        return body

    def test_valid_aeo_has_no_errors(self) -> None:
        _, _, issues = SuiteValidator().validate(self._aeo())
        assert not [i for i in issues if i.severity == "error"]

    def test_missing_entity_is_error(self) -> None:
        body = self._aeo()
        del body["entity"]
        _, _, issues = SuiteValidator().validate(body)
        assert any(i.kind == "aeo_entity_missing" for i in issues)

    def test_missing_entity_field_flagged(self) -> None:
        body = self._aeo(entity={"id": "https://x/", "type": "Organization"})  # no name
        _, _, issues = SuiteValidator().validate(body)
        assert any(i.kind == "aeo_entity_name_missing" for i in issues)

    def test_missing_authority_is_error(self) -> None:
        body = self._aeo()
        del body["authority"]
        _, _, issues = SuiteValidator().validate(body)
        assert any(i.kind == "aeo_primary_sources_missing" for i in issues)


class TestAgentCard:
    def test_valid(self) -> None:
        _, _, issues = SuiteValidator().validate(
            {"agent_card_version": "0.1", "agent_id": "tutor-bot", "capabilities": ["read"]}
        )
        assert not [i for i in issues if i.severity == "error"]

    def test_missing_capabilities(self) -> None:
        _, _, issues = SuiteValidator().validate({"agent_card_version": "0.1", "agent_id": "tutor-bot"})
        assert any(i.kind == "capabilities_missing" for i in issues)


class TestToolCard:
    def test_missing_required_fields(self) -> None:
        _, _, issues = SuiteValidator().validate({"tool_card_version": "0.1"})
        kinds = {i.kind for i in issues}
        assert {"tool_id_missing", "name_missing", "description_missing"} <= kinds


class TestIncidentCard:
    def test_missing_fields(self) -> None:
        _, _, issues = SuiteValidator().validate({"incident_card_version": "0.1"})
        assert any(i.kind == "incident_id_missing" for i in issues)
        assert any(i.kind == "summary_missing" for i in issues)

    def test_empty_affected_documents_is_warning(self) -> None:
        _, _, issues = SuiteValidator().validate(
            {
                "incident_card_version": "0.1",
                "incident_id": "x",
                "summary": "test",
                "severity": "low",
                "affected_documents": [],
            }
        )
        assert any(i.kind == "affected_documents_empty" and i.severity == "warning" for i in issues)


class TestDecisionCard:
    def test_status_with_conditions_required_but_missing(self) -> None:
        _, _, issues = SuiteValidator().validate(
            {
                "decision_card_version": "0.1",
                "decision_id": "x",
                "buyer": {"name": "B", "type": "organization"},
                "decision": {"status": "approved-with-conditions"},
                "subject": {"vendor_name": "V"},
                "rationale": "R",
            }
        )
        assert any(i.kind == "conditions_required" for i in issues)

    def test_clean_approved_card_has_no_errors(self) -> None:
        _, _, issues = SuiteValidator().validate(
            {
                "decision_card_version": "0.1",
                "decision_id": "x",
                "buyer": {"name": "B", "type": "organization"},
                "decision": {"status": "approved"},
                "subject": {"vendor_name": "V"},
                "rationale": "R",
            }
        )
        assert not [i for i in issues if i.severity == "error"]
