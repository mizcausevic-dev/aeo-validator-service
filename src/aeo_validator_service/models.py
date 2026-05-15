"""
Pydantic v2 models — what the validator hands back and what watches look like.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SpecKind = Literal[
    "aeo",
    "agent-card",
    "prompt-provenance",
    "ai-evidence",
    "tool-card",
    "tutor-card",
    "student-ai-disclosure",
    "classroom-aup",
    "clinical-ai",
    "incident-card",
    "decision-card",
    "unknown",
]
"""Eleven Kinetic Gain specs + `unknown` for anything else."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationIssue(StrictModel):
    severity: Literal["error", "warning"]
    field: str | None = None
    kind: str
    message: str


class ValidationResult(StrictModel):
    """One validation pass against a single document."""

    url: str
    fetched_at: str
    content_hash: str = Field(..., description="sha256:<hex> over canonical JSON.")
    spec: SpecKind
    spec_version: str | None = None
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    body: dict[str, Any] | None = Field(
        default=None,
        description="The fetched body. Omitted from /watches outputs to keep them small.",
    )


class DriftReport(StrictModel):
    """
    What changed between two validation results.

    `before` is the previous result for the URL; `after` is the current.
    """

    url: str
    drifted: bool
    spec_changed: bool
    became_invalid: bool
    became_valid: bool
    content_hash_before: str | None
    content_hash_after: str
    added_fields: list[str] = Field(default_factory=list)
    removed_fields: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    before_issues: int = 0
    after_issues: int = 0


class Watch(StrictModel):
    """
    A persistent watch on a URL. Holds the history of validation results so
    drift comparisons have somewhere to anchor.
    """

    watch_id: str
    url: str
    spec_hint: SpecKind | None = None
    last_result: ValidationResult | None = None
    history_count: int = 0
    created_at: str
