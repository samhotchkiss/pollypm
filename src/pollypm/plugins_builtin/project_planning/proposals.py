"""Planner improvement proposals — issue #275.

Alongside the primary plan, the architect (cold-start or replan) can
emit a list of improvement proposals: optional, bite-sized suggestions
the user chooses to accept or reject one at a time. Each proposal lands
in the cockpit inbox as its own task so the user gets a single decision
to make rather than a wall of text to triage.

This module owns three concerns:

1. The :class:`ImprovementProposal` dataclass — the shape the architect
   (or a fixture in tests) hands to the emitter.
2. :func:`emit_proposals` — persist each accepted proposal as an inbox
   task via the work service, tagged with ``proposal`` labels the TUI
   detects.
3. :func:`filter_rejected` — drop proposals the user previously
   rejected, so we don't re-notify them on every replan.

The emitter is the "plumbing" piece referenced in issue #275: even while
the architect LLM's improvements channel is still being wired, tests
and ``pm project replan`` flows can invoke this function with a hand-
built proposal list and see the inbox behaviour end-to-end.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Severity values accepted on a proposal. Kept deliberately short so the
# label suffix stays readable in the inbox row.
VALID_SEVERITIES: tuple[str, ...] = ("info", "advisory", "important")


@dataclass(slots=True)
class ImprovementProposal:
    """One improvement suggestion the architect surfaces for user review.

    Fields:

    * ``title`` — short summary (becomes the inbox subject).
    * ``rationale`` — the "why" paragraph.
    * ``proposed_task_spec`` — dict that seeds a work_tasks row if the
      user accepts. ``description`` and ``acceptance_criteria`` are the
      primary fields; extra keys are preserved verbatim for future
      growth.
    * ``severity`` — one of :data:`VALID_SEVERITIES`.
    * ``planner_memory_key`` — stable id used to remember rejections.
      If omitted, :meth:`derive_memory_key` computes a hash from title
      + project at emit time.
    """

    title: str
    rationale: str
    proposed_task_spec: dict = field(default_factory=dict)
    severity: str = "advisory"
    planner_memory_key: str = ""

    def derive_memory_key(self, project_key: str) -> str:
        """Return a stable per-(project, title) identifier for rejection memory."""
        if self.planner_memory_key:
            return self.planner_memory_key
        raw = f"{project_key}\x00{self.title.strip().lower()}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Markdown preview rendering
# ---------------------------------------------------------------------------


def render_proposal_body(proposal: ImprovementProposal) -> str:
    """Build the inbox-task body for an improvement proposal.

    The body starts with the rationale paragraph and is followed by a
    compact markdown preview of ``proposed_task_spec`` so the user can
    assess the shape of the task they'd be opting into.
    """
    sections: list[str] = []
    rationale = (proposal.rationale or "").strip()
    if rationale:
        sections.append(rationale)

    spec = proposal.proposed_task_spec or {}
    if spec:
        sections.append("")  # blank line
        sections.append("## Proposed task")
        title = (spec.get("title") or proposal.title or "").strip()
        if title:
            sections.append(f"- **title**: {title}")
        description = (spec.get("description") or "").strip()
        if description:
            sections.append("- **description**:")
            for line in description.splitlines():
                sections.append(f"  {line}")
        ac = (spec.get("acceptance_criteria") or "").strip()
        if ac:
            sections.append("- **acceptance criteria**:")
            for line in ac.splitlines():
                sections.append(f"  {line}")
        # Passthrough extras so a richer architect spec doesn't silently
        # get dropped from the preview.
        extras = {
            k: v for k, v in spec.items()
            if k not in ("title", "description", "acceptance_criteria")
        }
        if extras:
            sections.append("- **extra**:")
            sections.append("  " + json.dumps(extras, sort_keys=True, default=str))
    return "\n".join(sections).strip()


# ---------------------------------------------------------------------------
# Label conventions
# ---------------------------------------------------------------------------


PROPOSAL_LABEL = "proposal"


def proposal_labels(
    proposal: ImprovementProposal, project_key: str,
) -> list[str]:
    """Labels emitted on the inbox task for this proposal.

    Order matters only for readability — the TUI looks up by membership
    / prefix, not by index.
    """
    severity = proposal.severity if proposal.severity in VALID_SEVERITIES else "advisory"
    memkey = proposal.derive_memory_key(project_key)
    return [
        PROPOSAL_LABEL,
        f"project:{project_key}",
        f"severity:{severity}",
        f"memkey:{memkey}",
    ]


def is_proposal_task(task) -> bool:
    """Return True when a work_tasks row was emitted as a proposal."""
    try:
        labels = list(task.labels or [])
    except AttributeError:
        return False
    return PROPOSAL_LABEL in labels


def memkey_from_labels(labels: Iterable[str]) -> str | None:
    """Pull the ``memkey:<hash>`` label off a task, if present."""
    for label in labels or []:
        if isinstance(label, str) and label.startswith("memkey:"):
            return label.split(":", 1)[1]
    return None


def severity_from_labels(labels: Iterable[str]) -> str | None:
    for label in labels or []:
        if isinstance(label, str) and label.startswith("severity:"):
            return label.split(":", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit_proposals(
    service,
    *,
    project_key: str,
    proposals: list[ImprovementProposal],
    memory_path: Path | None = None,
) -> list[str]:
    """Persist ``proposals`` as inbox tasks on ``service``.

    * Previously-rejected memory keys are filtered before emission.
    * Each surviving proposal becomes one ``chat`` task with
      ``proposal`` labels; the TUI handles Accept/Reject from there.

    Returns the list of task_ids created (may be shorter than
    ``proposals`` when rejections were filtered out).
    """
    kept = filter_rejected(proposals, project_key=project_key, memory_path=memory_path)
    created: list[str] = []
    for proposal in kept:
        body = render_proposal_body(proposal)
        labels = proposal_labels(proposal, project_key)
        task = service.create(
            title=proposal.title,
            description=body,
            type="task",
            project=project_key,
            flow_template="chat",
            roles={"requester": "user", "operator": "architect"},
            priority="normal",
            created_by="architect",
            labels=labels,
        )
        created.append(task.task_id)
    return created


# ---------------------------------------------------------------------------
# Rejection filter
# ---------------------------------------------------------------------------


def filter_rejected(
    proposals: list[ImprovementProposal],
    *,
    project_key: str,
    memory_path: Path | None = None,
) -> list[ImprovementProposal]:
    """Return only proposals the user has not previously rejected."""
    from pollypm.plugins_builtin.project_planning.memory import is_proposal_rejected

    kept: list[ImprovementProposal] = []
    for proposal in proposals:
        memkey = proposal.derive_memory_key(project_key)
        if is_proposal_rejected(
            project_key=project_key,
            planner_memory_key=memkey,
            path=memory_path,
        ):
            continue
        kept.append(proposal)
    return kept


# ---------------------------------------------------------------------------
# Accept helper — used by the cockpit UI
# ---------------------------------------------------------------------------


def accept_proposal(
    service,
    *,
    task_id: str,
    proposal_spec: dict[str, Any],
    project_key: str,
    actor: str = "user",
):
    """Create a follow-on work_tasks row for an accepted proposal.

    Returns the newly-created :class:`Task`. The caller is expected to
    archive the inbox row and record a ``proposal_accepted`` context
    entry on it separately.
    """
    spec = proposal_spec or {}
    title = (spec.get("title") or "").strip() or "Proposal follow-up"
    description = (spec.get("description") or "").strip()
    acceptance_criteria = spec.get("acceptance_criteria") or None
    # The user-review flow has reviewer as ``actor_type: human``, so no
    # ``reviewer=`` role assignment is needed. Previously this used the
    # ``standard`` flow with ``reviewer=user`` — that's the savethenovel
    # bug shape (``user`` is not an autonomous agent that can claim a
    # role-typed review node). The user-review flow is the structurally
    # correct way to express "worker implements, human reviews".
    task = service.create(
        title=title,
        description=description,
        type="task",
        project=project_key,
        flow_template="user-review",
        roles={"worker": "worker", "requester": "user"},
        priority="normal",
        created_by=actor,
        acceptance_criteria=acceptance_criteria,
        labels=["from_proposal", f"project:{project_key}"],
    )
    # Context entry on the ORIGINATING inbox task is the caller's job
    # (``task_id`` above) — that way they can use their own service
    # handle and keep transaction boundaries obvious.
    return task
