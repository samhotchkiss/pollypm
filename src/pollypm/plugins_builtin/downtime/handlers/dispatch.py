"""Exploration dispatch — route a Candidate to the right handler.

Five categories (spec §6):

    kind                 | handler
    -------------------- | -----------------------------
    spec_feature         | handlers.spec_feature.run_spec_feature
    build_speculative    | handlers.build_speculative.run_build_speculative
    audit_docs           | handlers.audit_docs.run_audit_docs
    security_scan        | handlers.security_scan.run_security_scan
    try_alt_approach     | handlers.try_alt_approach.run_try_alt_approach

The dispatch layer is pure routing — it does no session spawning, no
LLM calls. dt05 ships scaffolds; dt06's apply step consumes the
structured result and routes to the appropriate commit/archive action.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.downtime.handlers.audit_docs import (
    AuditDocsResult,
    run_audit_docs,
)
from pollypm.plugins_builtin.downtime.handlers.build_speculative import (
    BuildSpeculativeResult,
    run_build_speculative,
)
from pollypm.plugins_builtin.downtime.handlers.pick_candidate import Candidate
from pollypm.plugins_builtin.downtime.handlers.security_scan import (
    SecurityScanResult,
    run_security_scan,
)
from pollypm.plugins_builtin.downtime.handlers.spec_feature import (
    SpecFeatureResult,
    run_spec_feature,
)
from pollypm.plugins_builtin.downtime.handlers.try_alt_approach import (
    TryAltApproachResult,
    run_try_alt_approach,
)


ExplorationResult = (
    SpecFeatureResult
    | BuildSpeculativeResult
    | AuditDocsResult
    | SecurityScanResult
    | TryAltApproachResult
)


class UnknownCategoryError(ValueError):
    """Raised when a Candidate's kind doesn't map to a known handler."""


def run_exploration(
    candidate: Candidate,
    *,
    project_root: Path,
) -> ExplorationResult:
    """Dispatch to the appropriate handler.

    The handler returns a dataclass specific to its category — callers
    typically serialise via :func:`result_to_dict` when writing to the
    work-service done output.
    """
    if candidate.kind == "spec_feature":
        return run_spec_feature(
            project_root=project_root,
            title=candidate.title,
            description=candidate.description,
            source=candidate.source,
        )
    if candidate.kind == "build_speculative":
        return run_build_speculative(
            project_root=project_root,
            title=candidate.title,
            description=candidate.description,
        )
    if candidate.kind == "audit_docs":
        return run_audit_docs(
            project_root=project_root,
            title=candidate.title,
            description=candidate.description,
        )
    if candidate.kind == "security_scan":
        return run_security_scan(
            project_root=project_root,
            title=candidate.title,
            description=candidate.description,
        )
    if candidate.kind == "try_alt_approach":
        return run_try_alt_approach(
            project_root=project_root,
            title=candidate.title,
            description=candidate.description,
        )
    raise UnknownCategoryError(
        f"Unknown downtime candidate kind: {candidate.kind!r}. Expected one of "
        "spec_feature, build_speculative, audit_docs, security_scan, try_alt_approach."
    )


def result_to_dict(result: ExplorationResult) -> dict[str, Any]:
    """Serialise a handler result to the structured done-output shape."""
    return asdict(result)
