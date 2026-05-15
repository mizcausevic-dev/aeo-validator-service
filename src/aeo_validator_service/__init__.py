"""
aeo-validator-service — always-on AEO + Kinetic Gain Protocol Suite validator.

The fourth layer of the AEO Reference Stack:

    1. SDKs (aeo-sdk-python / -typescript / -rust / -go / -swift)
    2. CLI  (aeo-cli)
    3. Crawler (aeo-crawler)
 -> 4. Validator service (this repo) — fetches a vendor URL, validates the
       document, hashes it canonically, and tracks drift across check-ins.

What the CLI doesn't give you that this service does:

    - HTTP API for non-Python callers
    - Persistent per-URL history of content_hash + validation_result
    - Drift detection: "did this vendor's AEO change since the last check?"
    - Diff output that points at the field-level change
    - Scheduled re-validation (POST /watches, then GET /watches/{id})

The service knows how to validate every spec in the Suite by sniffing the
top-level `*_version` field, the same trick the unified visualizer uses.
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

__version__ = "0.1.0"

__all__ = [
    "DriftReport",
    "SpecKind",
    "SuiteValidator",
    "ValidationIssue",
    "ValidationResult",
    "Watch",
    "__version__",
]
