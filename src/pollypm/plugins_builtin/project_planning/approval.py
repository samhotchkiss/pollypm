"""Present-plan-to-user approval helpers (pp08 / spec §3 stage 7).

Stage 7 is the **single** human touchpoint in the planning flow.
Everything upstream is autonomous; everything downstream waits on the
user's go/no-go. The plan_project flow already parks here with
``actor_type=human``; the flow engine handles the waiting state.

This module owns the adjacent contract:

1. Before the architect advances into stage 7, the three artifacts
   must be written and non-empty:
   - ``docs/project-plan.md``
   - a Risk Ledger section inside that file (or a sibling file)
   - ``docs/planning-session-log.md``
2. When the user rejects, the architect returns to stage 6
   (synthesize) with the user's rejection reason folded into the
   session log — ``record_rejection`` appends a narrative entry.
3. When the user approves, the architect advances to stage 8 (emit) —
   ``record_approval`` appends a corresponding entry so the session
   log always narrates what the user decided.

Stage 7 itself has no timeout — the plan waits indefinitely for the
user (spec §3 stage 7). No budget value on this node; no heartbeat
nudge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PLAN_PATH = "docs/project-plan.md"
SESSION_LOG_PATH = "docs/planning-session-log.md"

# The Risk Ledger section heading the architect must emit inside the
# plan document. A separate file is also accepted; if neither is
# present the gate fails.
RISK_LEDGER_HEADING = "## Risk Ledger"
RISK_LEDGER_PATH = "docs/project-plan-risk-ledger.md"


@dataclass(slots=True)
class ApprovalReadiness:
    """Result of checking whether the architect is ready to park at stage 7.

    ``ready`` is the aggregate decision; ``missing`` names each
    artifact that failed the non-empty / presence check so the
    architect can act on specific feedback rather than guess.
    """

    ready: bool
    missing: list[str]


def check_plan_ready_for_user(project_root: str | Path) -> ApprovalReadiness:
    """Verify the three stage-7 artifacts exist and are non-empty."""
    root = Path(project_root)
    missing: list[str] = []

    plan_path = root / PLAN_PATH
    if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8").strip():
        missing.append(PLAN_PATH)
    else:
        # Risk Ledger must appear either as a section in the plan or as
        # a sibling file. Either is fine — the architect picks.
        plan_text = plan_path.read_text(encoding="utf-8")
        sibling = root / RISK_LEDGER_PATH
        ledger_in_plan = RISK_LEDGER_HEADING in plan_text
        ledger_sibling = (
            sibling.is_file()
            and sibling.read_text(encoding="utf-8").strip() != ""
        )
        if not (ledger_in_plan or ledger_sibling):
            missing.append(
                f"Risk Ledger (expected '{RISK_LEDGER_HEADING}' section "
                f"in {PLAN_PATH} or non-empty {RISK_LEDGER_PATH})"
            )

    log_path = root / SESSION_LOG_PATH
    if not log_path.is_file() or not log_path.read_text(encoding="utf-8").strip():
        missing.append(SESSION_LOG_PATH)

    return ApprovalReadiness(ready=not missing, missing=missing)


def _append_to_session_log(project_root: str | Path, entry: str) -> Path:
    """Append a narrative entry to the session log (creates if absent)."""
    path = Path(project_root) / SESSION_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_text = f"{existing}\n### {now} — {entry.rstrip()}\n"
    path.write_text(new_text, encoding="utf-8")
    return path


def record_approval(
    project_root: str | Path,
    *,
    actor: str = "user",
    note: str | None = None,
) -> Path:
    """Append a stage-7 approval entry to the session log.

    ``note`` is the optional user comment (from ``pm task comment``
    before approve). When present it is included verbatim so the
    architect can honour any adjustments at stage 8.
    """
    entry = f"Stage 7 approval received from {actor}"
    if note:
        entry += f"\n\nUser note:\n\n{note.strip()}\n"
    else:
        entry += " (no user note)."
    return _append_to_session_log(project_root, entry)


def record_rejection(
    project_root: str | Path,
    *,
    actor: str = "user",
    reason: str,
) -> Path:
    """Append a stage-7 rejection entry to the session log.

    Rejection sends the architect back to stage 6 (synthesize) with
    the reason surfaced. Per spec, a rejection with no reason is
    accepted but discouraged — the log says so explicitly.
    """
    if not reason or not reason.strip():
        reason_text = "(no reason supplied — planning run ends)"
    else:
        reason_text = reason.strip()
    entry = (
        f"Stage 7 rejection from {actor}\n\n"
        f"Reason:\n\n{reason_text}\n"
    )
    return _append_to_session_log(project_root, entry)
