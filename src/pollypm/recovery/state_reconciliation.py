"""Flow-state reconciliation — detect observable drift between a task's
current flow node and the deliverables sitting on disk / in the ledger.

Dogfood surfaced a class of failure the heartbeat couldn't catch: an
agent writes the plan artifacts, fires a ``pm notify``, ends its turn,
but the work-service task node never advances. Everything looks done
except the one row of state the rest of the system reads from.

This module owns the ``reconcile_expected_advance`` helper — a pure
function that inspects the task's flow, the project's working tree, and
the state-store ledger for ``inbox.message.created`` events, and
returns the node the task *should* be at (if drift is detected) or
``None`` when nothing's amiss.

V1 scope — **log + alert only**. The heartbeat sweep raises a
``state_drift`` alert and records a ``state_drift`` event so the
operator sees the divergence; it does **not** auto-advance the task.
Auto-advance is explicitly a v2 concern (``[recovery].auto_reconcile``
flag) so we don't silently mutate work_tasks in response to a heuristic.

Heuristics (start narrow; this is the first pass):

* For tasks on the ``plan_project`` flow:
    - ``<project_path>/docs/plan/plan.md`` OR
      ``<project_path>/docs/project-plan.md`` exists AND is > 500
      bytes of non-whitespace content → observable deliverables say
      the ``synthesize`` stage has finished.
    - An ``inbox.message.created`` event exists in the last hour whose
      message mentions the plan being ready ("plan ready",
      "plan is ready", "plan ready for approval") → observable
      signal that the agent already announced completion, so the
      intended next stage is ``user_approval``.
    - Both → reconciliation target is ``user_approval``.
    - Plan file only, no notify → target is ``synthesize`` (the plan
      exists but the announcement hasn't landed yet; the agent is
      mid-synthesize and needs a nudge to close out the node).

* For any task:
    - If the task's current node carries an ``artifact_gate`` that
      points to a file, and that file exists with non-trivial
      content, the node is observably done. (Left as a hook — no
      flow currently uses ``artifact_gate``; the function is ready
      for when one does.)

* **Never** auto-advance past a human-review node. When the target
  node is ``user_approval`` (or any node with ``actor_type=human``
  in v2), the helper still returns the action — but the sweep
  handler raises an alert rather than mutating state.

The function is pure: take ``(task, project_path, work_service,
state_store=None)`` → return ``ReconciliationAction | None``. Easy to
unit-test without live sessions or a running work-service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


# Minimum non-whitespace byte count before a plan file counts as a
# real plan. Mirrors ``plan_presence.MIN_PLAN_SIZE_BYTES`` so the two
# gates stay consistent — if you bump one, bump the other.
MIN_PLAN_SIZE_BYTES = 500

# Candidate plan-file locations scanned in order. The first match wins.
# Shared with the plan-presence gate, the advisor context pack, and
# the cockpit plan-review widget — see
# :data:`pollypm.plugins_builtin.project_planning.plan_presence.CANONICAL_PLAN_RELATIVE_PATHS`
# for the authoritative tuple.
from pollypm.plugins_builtin.project_planning.plan_presence import (
    CANONICAL_PLAN_RELATIVE_PATHS as _PLAN_FILE_CANDIDATES,
)

# Window for matching a "plan ready" notify against the current sweep.
# 1h is generous — the architect's turn might have ended anywhere from
# a minute to 30min before the next sweep tick — but tight enough that
# a stale notify from last week doesn't cause a false positive.
_NOTIFY_LOOKBACK_SECONDS = 3600

# Keywords / phrases that mark an ``inbox.message.created`` event as
# the architect announcing plan completion. Case-insensitive substring
# match; any single hit promotes the classification to ``user_approval``.
_PLAN_READY_PHRASES: tuple[str, ...] = (
    "plan ready for approval",
    "plan is ready",
    "plan ready",
    "ready for approval",
)


@dataclass(slots=True, frozen=True)
class ReconciliationAction:
    """Observable drift → the node the task should be on.

    ``advance_to_node`` is the inferred current node based on
    observable deliverables (files on disk, events in the ledger).
    ``reason`` is human-readable — it shows up in the event message
    and the alert body so Sam can decide case-by-case.
    """

    advance_to_node: str
    reason: str


def _plan_file_present(project_path: Path) -> tuple[Path | None, int]:
    """Return the first plan file that exists with > 500 bytes, or ``(None, 0)``.

    Fails closed on any IO error — we'd rather skip reconciliation than
    raise a spurious drift alert because the filesystem was momentarily
    unhappy.
    """
    for candidate in _PLAN_FILE_CANDIDATES:
        path = project_path / candidate
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = len(text.strip())
        if stripped > MIN_PLAN_SIZE_BYTES:
            return path, stripped
    return None, 0


def _plan_ready_notify_recent(
    state_store: Any,
    project_key: str,
    *,
    now: datetime | None = None,
    lookback_seconds: int = _NOTIFY_LOOKBACK_SECONDS,
) -> bool:
    """True iff a recent ``inbox.message.created`` event names plan-ready.

    Scans the ledger for events in the last ``lookback_seconds`` whose
    message mentions the project **and** contains a plan-ready phrase.
    Case-insensitive; any one phrase hit is enough. Returns False on
    any error — no drift alert should trigger because we couldn't
    read the ledger.
    """
    if state_store is None:
        return False
    cutoff_dt = (now or datetime.now(UTC)) - timedelta(seconds=lookback_seconds)
    query_messages = getattr(state_store, "query_messages", None)
    if not callable(query_messages):
        return False
    try:
        rows = query_messages(type="event", since=cutoff_dt, limit=50)
    except Exception:  # noqa: BLE001
        return False
    if not rows:
        return False
    project_key_lc = (project_key or "").lower()
    for row in rows:
        payload = row.get("payload") if isinstance(row, dict) else None
        event_type = str(row.get("subject") or "") if isinstance(row, dict) else ""
        if not event_type and isinstance(payload, dict):
            event_type = str(payload.get("event_type") or "")
        if event_type != "inbox.message.created":
            continue
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
        if not message and isinstance(row, dict):
            message = str(row.get("body") or "")
        message = message.lower()
        if not any(phrase in message for phrase in _PLAN_READY_PHRASES):
            continue
        # Soft project match — the notify carries the project key in
        # the activity_summary body. When the project key is empty
        # (unusual) we accept any match; otherwise require the key
        # appear in the message. This keeps a plan-ready notify on
        # project A from triggering drift on project B.
        if project_key_lc and project_key_lc not in message:
            continue
        return True
    return False


def _flow_template_for_task(task: Any, work_service: Any) -> Any | None:
    """Resolve the flow template bound to ``task``.

    Returns ``None`` on any failure — reconciliation then skips the
    per-node artifact_gate heuristic and returns None for anything
    that depends on node metadata. The plan_project heuristic does
    not need the template (it hard-codes the node names).
    """
    flow_id = getattr(task, "flow_template_id", None)
    if not flow_id or work_service is None:
        return None
    try:
        return work_service.get_flow(
            flow_id, project=getattr(task, "project", None),
        )
    except Exception:  # noqa: BLE001
        return None


def _artifact_gate_satisfied(
    task: Any, project_path: Path, work_service: Any,
) -> ReconciliationAction | None:
    """Check the current node's artifact_gate (if any) against disk.

    The ``artifact_gate`` concept is forward-looking — no flow uses it
    in the first cut. The hook is here so when a flow adds one, the
    reconciler will honour it without a code change elsewhere. Expected
    shape on a ``FlowNode``:

        gates: ["artifact_gate:docs/whatever.md"]

    Where the ``:<path>`` suffix names a file relative to the project
    root. Presence + non-trivial content → the current node is done
    and the reconciler proposes advancing to ``next_node_id``.
    """
    template = _flow_template_for_task(task, work_service)
    if template is None:
        return None
    current = getattr(task, "current_node_id", None)
    if not current:
        return None
    node = template.nodes.get(current) if hasattr(template, "nodes") else None
    if node is None:
        return None
    gates = getattr(node, "gates", None) or []
    for gate in gates:
        if not isinstance(gate, str) or not gate.startswith("artifact_gate:"):
            continue
        rel = gate.split(":", 1)[1].strip()
        if not rel:
            continue
        target = project_path / rel
        try:
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text.strip()) <= MIN_PLAN_SIZE_BYTES:
            continue
        next_node = getattr(node, "next_node_id", None)
        if not next_node:
            return None
        return ReconciliationAction(
            advance_to_node=next_node,
            reason=(
                f"artifact_gate satisfied: {rel} has "
                f"{len(text.strip())} bytes — node {current} observably done"
            ),
        )
    return None


def reconcile_expected_advance(
    task: Any,
    project_path: Path,
    work_service: Any,
    *,
    state_store: Any = None,
    now: datetime | None = None,
) -> ReconciliationAction | None:
    """Return a :class:`ReconciliationAction` when drift is detected, else None.

    Pure function — takes a task, the project's filesystem root, a
    handle to the work service (for flow-template lookup), and
    optionally the unified message store (for event-ledger heuristics). All of
    the disk / ledger reads are soft-fail: any exception yields
    ``None`` rather than a false positive.

    See module docstring for the heuristic catalogue. When multiple
    heuristics match, the first one wins in the order documented
    there — ``plan_project`` specialisation before the generic
    ``artifact_gate`` check.
    """
    if task is None or project_path is None:
        return None

    flow_id = getattr(task, "flow_template_id", "") or ""
    current_node = getattr(task, "current_node_id", "") or ""
    project_key = getattr(task, "project", "") or ""

    # Plan-project heuristic. Only meaningful when the task is actually
    # on the plan_project flow and sitting upstream of user_approval.
    if flow_id == "plan_project":
        # Nodes upstream of user_approval where drift is plausible.
        # Once the architect is at user_approval (or past it) the
        # node IS where we'd reconcile to — nothing to do.
        _UPSTREAM_NODES = {
            "research", "discover", "decompose", "test_strategy",
            "magic", "critic_panel", "synthesize",
        }
        if current_node in _UPSTREAM_NODES:
            plan_file, size = _plan_file_present(project_path)
            notify_fired = _plan_ready_notify_recent(
                state_store, project_key, now=now,
            )
            if plan_file is not None and notify_fired:
                return ReconciliationAction(
                    advance_to_node="user_approval",
                    reason=(
                        f"plan file {plan_file.name} present ({size} bytes) "
                        f"and plan-ready notify fired in the last hour — "
                        f"node {current_node} observably past synthesize"
                    ),
                )
            if plan_file is not None:
                return ReconciliationAction(
                    advance_to_node="synthesize",
                    reason=(
                        f"plan file {plan_file.name} present ({size} bytes) "
                        f"— node {current_node} observably past earlier stages"
                    ),
                )

    # Generic artifact_gate heuristic (flow-agnostic).
    action = _artifact_gate_satisfied(task, project_path, work_service)
    if action is not None:
        return action

    return None
