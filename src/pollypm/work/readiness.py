"""Task readiness coaching.

Readiness is intentionally advisory for v1. The work service still uses
hard gates for empty/no-op tasks, while these helpers describe gaps the PM
or worker should see before execution starts.
"""

from __future__ import annotations

from typing import Iterable


def readiness_warnings(task) -> list[str]:
    """Return non-blocking readiness warnings for a task-like object."""
    warnings: list[str] = []
    description = (getattr(task, "description", None) or "").strip()
    acceptance = (getattr(task, "acceptance_criteria", None) or "").strip()
    constraints = (getattr(task, "constraints", None) or "").strip()
    relevant_files = list(getattr(task, "relevant_files", []) or [])
    haystack = " ".join(
        part.lower()
        for part in (description, acceptance, constraints)
        if part
    )

    if not acceptance:
        warnings.append("missing acceptance criteria")
    if "test" not in haystack and "verify" not in haystack:
        warnings.append("missing verification expectation")
    if not relevant_files and "discover files" not in haystack:
        warnings.append("missing relevant files or explicit 'discover files'")
    return warnings


def format_readiness_warnings(warnings: Iterable[str]) -> str:
    """Render readiness warnings as one compact sentence."""
    items = [item for item in warnings if item]
    if not items:
        return ""
    return "Task is queueable but underspecified: " + "; ".join(items) + "."
