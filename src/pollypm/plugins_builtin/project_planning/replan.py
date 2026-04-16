"""Drift analysis for ``pm project replan`` (pp09 / spec §8).

Reads existing plan artefacts, git history since last plan, and the
task-completion record from the work service, then produces a
structured drift analysis. The analysis is rendered as a chat task in
the user's inbox; the user decides whether to trigger a fresh
planning round on the delta.

This module is the pure-logic half: given inputs (file contents, task
records, commit list), it emits a ``DriftAnalysis`` dataclass and a
markdown rendering. pp10 wires the I/O half (reading the files,
shelling git, spawning the chat task).

Sections produced:

- **Gaps** — modules the plan named that never shipped.
- **Drift** — modules that shipped without the user-level test the
  plan required.
- **Materialized risks** — ledger entries whose risk actually
  happened, per the task/commit record.
- **New opportunities** — magic the architect sees now with fresh
  eyes. Free-form; the architect fills this in during replan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Heuristics — cheap, deliberately conservative. The architect refines
# at replan time when the chat task opens.

MODULE_LINE_PATTERN = re.compile(
    r"^\s*[-*]\s+\*\*(?P<name>[A-Za-z][\w\-\s]*?)\*\*",
    re.MULTILINE,
)
"""Match ``- **ModuleName** — ...`` list items inside a plan body.

The architect's prompt asks for module entries in this shape; anything
fancier still reads as module names for drift purposes."""


@dataclass(slots=True)
class ShippedRecord:
    """One completed task relevant to a prior plan.

    Pulled from the work service at replan time. ``module`` — best
    effort inference; callers may set this to an empty string if the
    task isn't obviously a module task.
    """

    task_id: str
    title: str
    module: str
    user_level_test_receipt: bool = False


@dataclass(slots=True)
class CancelledRecord:
    task_id: str
    title: str
    module: str
    reason: str = ""


@dataclass(slots=True)
class DriftAnalysis:
    planned_modules: list[str] = field(default_factory=list)
    shipped_modules: list[str] = field(default_factory=list)
    cancelled_modules: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    drift_modules: list[str] = field(default_factory=list)
    materialized_risks: list[str] = field(default_factory=list)
    new_opportunities: list[str] = field(default_factory=list)


def extract_plan_modules(plan_text: str) -> list[str]:
    """Pull module names out of the plan body.

    Returns names in first-occurrence order, deduplicated. Names are
    trimmed of whitespace but otherwise preserved so the drift
    analysis can match them verbatim against task titles.
    """
    seen: list[str] = []
    for match in MODULE_LINE_PATTERN.finditer(plan_text):
        name = match.group("name").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _module_match(name: str, title: str) -> bool:
    """Return True when a task title looks like it implements a module."""
    name_lc = name.lower()
    title_lc = title.lower()
    return name_lc in title_lc


def analyse_drift(
    *,
    plan_text: str,
    shipped: list[ShippedRecord],
    cancelled: list[CancelledRecord],
    ledger_risks: list[str] | None = None,
    materialized: list[str] | None = None,
) -> DriftAnalysis:
    """Compute the drift analysis structure.

    ``ledger_risks`` is the flat list of risks from the plan's Risk
    Ledger. ``materialized`` is the subset of those risks that the
    caller (via git / work-service signals) has flagged as actually
    happened. Keeping them as two separate inputs means the caller
    owns the detection heuristic; this module just presents the
    result.
    """
    planned = extract_plan_modules(plan_text)

    shipped_modules: list[str] = []
    drift_modules: list[str] = []
    for record in shipped:
        module_name = record.module or ""
        if not module_name:
            # Try to infer from title by matching against any planned module.
            for candidate in planned:
                if _module_match(candidate, record.title):
                    module_name = candidate
                    break
        if module_name:
            shipped_modules.append(module_name)
            if not record.user_level_test_receipt:
                drift_modules.append(module_name)

    cancelled_modules = [rec.module for rec in cancelled if rec.module]

    gaps = [
        module
        for module in planned
        if module not in shipped_modules and module not in cancelled_modules
    ]

    materialized_risks = list(materialized or [])
    # Defensive: only include risks that were actually in the ledger,
    # so drift reports don't invent materializations.
    if ledger_risks is not None:
        allowed = set(ledger_risks)
        materialized_risks = [r for r in materialized_risks if r in allowed]

    return DriftAnalysis(
        planned_modules=planned,
        shipped_modules=shipped_modules,
        cancelled_modules=cancelled_modules,
        gaps=gaps,
        drift_modules=drift_modules,
        materialized_risks=materialized_risks,
        new_opportunities=[],
    )


def render_drift_chat(analysis: DriftAnalysis) -> str:
    """Render a ``DriftAnalysis`` as a markdown chat task body.

    The body becomes the user-facing task description when
    ``pm project replan`` opens the chat. Deliberately punchy — the
    user reads this, decides, and acts.
    """
    lines = ["# Drift analysis", ""]
    if analysis.planned_modules:
        lines.append(
            f"Plan had {len(analysis.planned_modules)} module(s): "
            + ", ".join(analysis.planned_modules)
            + "."
        )
    else:
        lines.append("No modules parsed out of the plan; skipping module-level drift.")
    lines.append("")

    if analysis.gaps:
        lines.append("## Gaps — planned but not shipped")
        for module in analysis.gaps:
            lines.append(f"- {module}")
        lines.append("")

    if analysis.drift_modules:
        lines.append("## Drift — shipped without user-level test")
        for module in analysis.drift_modules:
            lines.append(f"- {module}")
        lines.append("")

    if analysis.cancelled_modules:
        lines.append("## Cancelled — explicitly dropped")
        for module in analysis.cancelled_modules:
            lines.append(f"- {module}")
        lines.append("")

    if analysis.materialized_risks:
        lines.append("## Materialized risks")
        for risk in analysis.materialized_risks:
            lines.append(f"- {risk}")
        lines.append("")

    if analysis.new_opportunities:
        lines.append("## New opportunities")
        for item in analysis.new_opportunities:
            lines.append(f"- {item}")
        lines.append("")

    lines.append(
        "Decide what to act on. Approve → re-run planning on the "
        "delta. Comment with adjustments → the architect will honour "
        "them on the next pass."
    )
    return "\n".join(lines)
