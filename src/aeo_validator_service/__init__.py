"""
aeo-validator-service — local HTTP smoke validator for Suite documents.

The fourth layer of the AEO Reference Stack:

    1. SDKs (aeo-sdk-python / -typescript / -rust / -go / -swift)
    2. CLI  (aeo-cli)
    3. Crawler (aeo-crawler)
 -> 4. Validator service (this repo) — fetches a vendor URL, validates the
       document, hashes it canonically, and tracks drift across check-ins.

What the CLI doesn't give you that this service does:

    - HTTP API for non-Python callers
    - Bounded, process-local per-URL history of content_hash + validation_result
    - Drift detection: "did this vendor's AEO change since the last check?"
    - Diff output that points at the field-level change
    - Caller-triggered re-validation (POST /watches/{id}/recheck)

The service sniffs eleven recognised top-level `*_version` fields and applies
shallow checks. It is not a full schema or authority validator.
"""

from __future__ import annotations

from .models import (
    DriftReport,
    SpecKind,
    ValidationIssue,
    ValidationResult,
    Watch,
)
from .validator import SuiteValidator

__version__ = "0.2.0"

__all__ = [
    "DriftReport",
    "SpecKind",
    "SuiteValidator",
    "ValidationIssue",
    "ValidationResult",
    "Watch",
    "__version__",
]
