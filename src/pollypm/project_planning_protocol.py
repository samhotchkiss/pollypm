"""Shared project-planning helpers consumed by core + the plugin (#1363).

Core modules (``cockpit_ui``) previously had to lazy-import
``pollypm.plugins_builtin.project_planning.proposals`` and
``pollypm.plugins_builtin.project_planning.memory`` to honour the
inbox's Accept/Reject keybindings on improvement-proposal rows. That
coupling makes the ``project_planning`` plugin effectively non-optional
— disabling it would break a core UI path silently.

This module hosts the small set of helpers the cockpit actually needs:

* :func:`memkey_from_labels` — pure label-list parser. No plugin
  coupling, no filesystem.
* :func:`record_proposal_rejection` / :func:`is_proposal_rejected` —
  append-only JSONL helpers backing the planner's rejection memory.
  File-IO only; no plugin internals.
* :func:`accept_proposal` — orchestrates a follow-on ``work_tasks`` row
  via the provided ``WorkService``. Takes the service through the
  argument list, so no plugin imports are needed.

The plugin re-exports these names from ``proposals`` / ``memory`` so
existing callers (tests, planner code, CLI) keep working without
churn. The plugin's ``memory.REJECTIONS_FILE`` re-exports the constant
defined here so both sides agree on the path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from pollypm.config import GLOBAL_CONFIG_DIR


# Canonical on-disk location for the planner's improvement-proposal
# rejection log. The plugin's ``memory`` module re-exports this as
# ``REJECTIONS_FILE`` for back-compat. Kept here (not in the plugin)
# because :func:`record_proposal_rejection` / :func:`is_proposal_rejected`
# live in this module and need a single source of truth.
REJECTIONS_FILE = GLOBAL_CONFIG_DIR / "memory" / "planner_rejections.jsonl"


def _rejections_path(override: Path | None = None) -> Path:
    """Return the active rejections file path (defaults to :data:`REJECTIONS_FILE`)."""
    return override if override is not None else REJECTIONS_FILE


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------


def memkey_from_labels(labels: Iterable[str]) -> str | None:
    """Pull the ``memkey:<hash>`` label off a task, if present."""
    for label in labels or []:
        if isinstance(label, str) and label.startswith("memkey:"):
            return label.split(":", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Rejection memory — append-only JSONL
# ---------------------------------------------------------------------------


def record_proposal_rejection(
    *,
    project_key: str,
    planner_memory_key: str,
    rationale: str = "",
    path: Path | None = None,
) -> Path:
    """Append a rejection record. Idempotent — duplicate rejections are a no-op.

    The file layout is append-only JSONL; each line carries the project
    key, the memkey, the optional free-form rationale, and a timestamp
    for future analytics. The predicate :func:`is_proposal_rejected`
    matches on (project_key, planner_memory_key) only, so re-adding the
    same pair is harmless but wastes a byte or two.
    """
    target = _rejections_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project": project_key,
        "planner_memory_key": planner_memory_key,
        "rationale": (rationale or "").strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_type": "proposal_rejected",
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False))
        fh.write("\n")
    return target


def is_proposal_rejected(
    *,
    project_key: str,
    planner_memory_key: str,
    path: Path | None = None,
) -> bool:
    """Predicate: has this (project, memkey) pair been rejected before?

    Returns False when the rejections file doesn't exist yet (fresh
    install). Malformed lines are skipped — the predicate degrades to
    "no rejection found" rather than crashing the planner.
    """
    target = _rejections_path(path)
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if (
            obj.get("project") == project_key
            and obj.get("planner_memory_key") == planner_memory_key
        ):
            return True
    return False


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

    Returns the newly-created ``Task``. The caller is expected to
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


__all__ = [
    "REJECTIONS_FILE",
    "accept_proposal",
    "is_proposal_rejected",
    "memkey_from_labels",
    "record_proposal_rejection",
]
