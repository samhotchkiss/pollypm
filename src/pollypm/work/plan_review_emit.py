"""Backstop emit of ``plan_review`` inbox cards for plan-shaped done tasks (#1511).

Today the only path that emits a ``plan_review`` notify message + inbox
task is the architect's prompt-driven ``pm notify --label plan_review``
call inside the ``plan_project`` flow's reflection node (#1399, #1400).
That works for projects that flow through ``plan_project`` end-to-end,
but a plan-shaped task built on the ``standard`` (or any other) flow
reaches ``done`` with no ``plan_review`` inbox row, so the cockpit's
2-line approval card never renders. ``coffeeboardnm`` was the live
witness — labels=``["visual-explainer", "research", "poc-plan"]``,
flow=``standard``, status=``done``, zero ``plan_review`` rows.

This module owns the *backstop* emit:

* :func:`is_plan_shaped_task` — conservative label-based heuristic.
  Returns True when a task carries a known plan label
  (``poc-plan`` / ``plan`` / ``project-plan``). Title-based detection
  was deliberately rejected — false positives that emit a phantom
  approval card are far worse than false negatives that leave a niche
  edge case uncovered.
* :func:`already_has_plan_review_message` — idempotence probe. Looks
  for an open ``plan_review`` notify message on the workspace ``messages``
  table whose labels include ``plan_task:<project>/<n>``. Used by both
  the post-done hook and the watchdog backfill so neither can double-
  emit.
* :func:`emit_plan_review_for_task` — best-effort emit. Mirrors the
  shape that ``cli_features.session_runtime.notify`` produces for the
  ``--label plan_review --label plan_task:<id>`` invocation: a
  ``messages`` row + a ``chat`` flow inbox task labeled identically.
  The task carries ``roles.requester=user``, ``roles.operator=architect``
  so the cockpit dashboard treats it as a notify-only inbox card and
  the ``plan_review`` label drives the 2-line render in
  ``cockpit_ui._format_inbox_plan_review_row``.

The transition hook in :mod:`pollypm.work.service_transitions.on_task_done`
calls :func:`maybe_emit_plan_review_on_task_done` after the existing
``check_and_flush_on_done`` + ``emit_task_shipped_card`` hooks. The
hook is no-op for the ``plan_project`` flow (its reflection node owns
the canonical emit) and for tasks that already have an open
plan_review row — both conditions are checked before any write.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pollypm.work.models import WorkStatus

logger = logging.getLogger(__name__)


# Conservative label set — a task carrying any of these labels is treated
# as "plan-shaped" for the purpose of the backstop emit. The set is
# deliberately small: every entry has been observed on real plan tasks
# (``poc-plan`` on coffeeboardnm/1, ``plan`` and ``project-plan`` on
# pre-#1399 plan_project tasks). Adding a new label here is fine; widening
# to title heuristics is not — see module docstring.
PLAN_SHAPED_LABELS: frozenset[str] = frozenset({
    "poc-plan",
    "plan",
    "project-plan",
})

# Flow templates whose own internal nodes already emit ``plan_review``
# (the architect's reflection node landed in #1399). The backstop must
# never duplicate those — Part A is purely *additive* coverage for
# flows that lack such a node.
_PLAN_OWNING_FLOWS: frozenset[str] = frozenset({
    "plan_project",
    "critique_flow",
})

# Label set the architect's ``pm notify --label plan_review`` call
# stamps on the message + task. Mirrors the contract documented in
# ``src/pollypm/plugins_builtin/project_planning/profiles/architect.md``.
PLAN_REVIEW_LABEL = "plan_review"
NOTIFY_LABEL = "notify"


def is_plan_shaped_task(task: Any) -> bool:
    """Return True when ``task`` looks like a plan task by labels.

    Conservative — only label matches count. Title heuristics were
    rejected because the false-positive cost (phantom approval cards
    on unrelated done tasks) is much higher than the false-negative
    cost (one more obscure flow not covered by the backstop).
    """
    labels = set(getattr(task, "labels", None) or [])
    return bool(labels & PLAN_SHAPED_LABELS)


def task_is_eligible_for_backstop(task: Any) -> bool:
    """Return True when ``task`` should get a backstop ``plan_review`` emit.

    Three conditions, all required:

    1. The task is plan-shaped by labels (:func:`is_plan_shaped_task`).
    2. The task is at ``work_status=done``. Plan-review is meaningful
       only after the architect's plan task has actually completed.
    3. The task is NOT on a plan-owning flow (``plan_project`` /
       ``critique_flow``) — those flows have their own reflection node
       and we must not duplicate it.
    """
    if not is_plan_shaped_task(task):
        return False
    status = getattr(task, "work_status", None)
    status_value = getattr(status, "value", status)
    if status_value != WorkStatus.DONE.value:
        return False
    flow_id = getattr(task, "flow_template_id", "") or ""
    if flow_id in _PLAN_OWNING_FLOWS:
        return False
    return True


def _plan_task_label(task_id: str) -> str:
    return f"plan_task:{task_id}"


def already_has_plan_review_message(
    *,
    db_path: Path,
    project: str,
    plan_task_id: str,
) -> bool:
    """Return True iff the workspace ``messages`` table has a plan_review row for the task.

    Scans messages with ``scope=project`` (and the legacy ``scope=inbox``
    bucket pre-#1425) that carry both the ``plan_review`` label AND a
    ``plan_task:<plan_task_id>`` sidecar. We deliberately accept any
    state — open, closed, replayed — because a previously-emitted
    plan_review row that has since been approved/rejected still
    constitutes "the user already saw a card for this plan task" and
    we should not re-surface a fresh card on every cadence tick.

    Best-effort: any error returns False so the emit path proceeds.
    Idempotence is enforced again at the writer boundary by the
    work-service ``messages`` table's row-level dedupe within the
    same transaction; this probe is the cheap pre-check.
    """
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: SQLAlchemyStore import failed", exc_info=True,
        )
        return False
    target_label = _plan_task_label(plan_task_id)
    seen_message_ids: set[Any] = set()
    try:
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: SQLAlchemyStore open failed for %s",
            db_path, exc_info=True,
        )
        return False
    try:
        scopes = [project] if project else []
        if "inbox" not in scopes:
            scopes.append("inbox")
        for scope in scopes:
            try:
                rows = store.query_messages(scope=scope) or []
            except Exception:  # noqa: BLE001
                logger.debug(
                    "plan_review_emit: query_messages failed for scope=%s",
                    scope, exc_info=True,
                )
                continue
            for row in rows:
                row_id = row.get("id")
                if row_id is not None:
                    if row_id in seen_message_ids:
                        continue
                    seen_message_ids.add(row_id)
                if (row.get("type") or "").lower() != "notify":
                    continue
                raw_labels = row.get("labels")
                if isinstance(raw_labels, str):
                    try:
                        raw_labels = json.loads(raw_labels)
                    except (TypeError, ValueError):
                        raw_labels = []
                labels = set(raw_labels or [])
                if PLAN_REVIEW_LABEL in labels and target_label in labels:
                    return True
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
    return False


def _build_plan_review_body(plan_task_id: str) -> str:
    """Render the body the architect normally writes via ``pm notify``.

    Mirrors ``profiles/architect.md`` plan_review_handoff template, minus
    the explainer pointer (the backstop emit doesn't synthesise an HTML
    explainer — the user opens the project drilldown surface from #1401
    instead).
    """
    return (
        f"A plan task ({plan_task_id}) reached done without a "
        f"plan_review handoff (backstop emit, see #1511).\n"
        "\n"
        "Open the project drilldown to read the plan and decide whether "
        "to approve, request changes, or discuss with the PM."
    )


def _build_plan_review_user_prompt(plan_task_id: str) -> dict[str, Any]:
    return {
        "summary": "A plan-shaped task completed without a plan_review handoff.",
        "steps": [
            "Open the project drilldown plan-review surface.",
            "Read the plan and any open decisions.",
            "Approve the plan, request changes, or discuss with the PM.",
        ],
        "question": (
            "Review the plan and decide whether to approve as-is, "
            "request changes, or discuss further."
        ),
        "actions": [
            {"label": "Review plan", "kind": "review_plan"},
            {"label": "Open task", "kind": "open_task", "task_id": plan_task_id},
        ],
    }


def emit_plan_review_for_task(
    *,
    svc: Any,
    task: Any,
    actor: str = "audit_watchdog",
    requester: str = "user",
) -> str | None:
    """Create the ``plan_review`` message + inbox task for ``task``.

    Mirrors the side-effects of ``pm notify --label plan_review --label
    plan_task:<id>`` (see :func:`pollypm.cli_features.session_runtime.notify`)
    so the cockpit inbox renders the row identically to the canonical
    emit path: a ``messages.notify`` row carrying the ``plan_review`` +
    ``plan_task:<id>`` labels, plus a chat-flow inbox task with the same
    labels and ``operator=<actor>`` / ``requester=<requester>`` roles.

    Idempotent — returns ``None`` (and emits nothing) when an existing
    plan_review row for the same plan_task_id is already present in the
    messages table. Best-effort on failure: logs and swallows so a
    broken inbox surface never crashes the transition hook or watchdog
    cadence.

    Returns the new inbox task_id on first emit, ``None`` on skip /
    failure.
    """
    plan_task_id = getattr(task, "task_id", None)
    project = getattr(task, "project", "") or ""
    if not plan_task_id or not project:
        return None
    db_path = getattr(svc, "_db_path", None)
    if db_path is None:
        logger.debug(
            "plan_review_emit: svc has no _db_path, skipping for %s", plan_task_id,
        )
        return None
    db_path = Path(db_path)

    if already_has_plan_review_message(
        db_path=db_path, project=project, plan_task_id=plan_task_id,
    ):
        return None

    subject = f"Plan ready for review: {project}"
    body = _build_plan_review_body(plan_task_id)
    target_label = _plan_task_label(plan_task_id)
    labels = [
        PLAN_REVIEW_LABEL,
        f"project:{project}",
        target_label,
    ]
    user_prompt = _build_plan_review_user_prompt(plan_task_id)

    # 1. Write the messages row (the canonical persisted record). The
    # cockpit inbox renderer reads from the messages table; the inbox
    # task below is the work-service surface that drives the [A]/[D]
    # keybindings.
    try:
        from pollypm.store import SQLAlchemyStore
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: SQLAlchemyStore import failed", exc_info=True,
        )
        return None
    try:
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: SQLAlchemyStore open failed for %s",
            db_path, exc_info=True,
        )
        return None
    message_id: int | None = None
    try:
        try:
            message_id = store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient=requester,
                sender=actor or "audit_watchdog",
                subject=subject,
                body=body,
                scope=project,
                labels=labels,
                payload={
                    "actor": actor or "audit_watchdog",
                    "project": project,
                    "milestone_key": None,
                    "requester": requester,
                    "user_prompt": user_prompt,
                    "plan_task_id": plan_task_id,
                    "backstop_source": "plan_review_emit",
                },
                state="closed",  # mirrors session_runtime.notify for immediate tier
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "plan_review_emit: enqueue_message failed for %s: %s",
                plan_task_id, exc, exc_info=True,
            )
            return None
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    # 2. Create the inbox task. The cockpit's plan-review surfaces
    # (#1400 / #1401) accept either the message row or the task row;
    # creating both keeps parity with the canonical ``pm notify`` path
    # so callers that filter by source ("message" vs "task") see the
    # same shape regardless of how the row landed.
    inbox_task_id: str | None = None
    try:
        task_labels = [
            *labels,
            NOTIFY_LABEL,
            f"notify_message:{message_id}" if message_id is not None else NOTIFY_LABEL,
        ]
        # Drop any duplicate labels while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for label in task_labels:
            if label in seen:
                continue
            seen.add(label)
            deduped.append(label)
        new_task = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project,
            flow_template="chat",
            roles={
                "requester": requester,
                "operator": actor or "audit_watchdog",
            },
            priority="high",
            created_by=actor or "audit_watchdog",
            labels=deduped,
        )
        inbox_task_id = getattr(new_task, "task_id", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: svc.create failed for %s: %s",
            plan_task_id, exc, exc_info=True,
        )
        return None

    # 3. Backfill the message payload with the new task_id so the cockpit
    # renderer can resolve message → task without a label scan.
    if message_id is not None and inbox_task_id is not None:
        try:
            store = SQLAlchemyStore(f"sqlite:///{db_path}")
        except Exception:  # noqa: BLE001
            logger.debug(
                "plan_review_emit: SQLAlchemyStore reopen failed for payload backfill",
                exc_info=True,
            )
            return inbox_task_id
        try:
            store.update_message(
                message_id,
                payload={
                    "actor": actor or "audit_watchdog",
                    "project": project,
                    "milestone_key": None,
                    "requester": requester,
                    "user_prompt": user_prompt,
                    "plan_task_id": plan_task_id,
                    "task_id": inbox_task_id,
                    "backstop_source": "plan_review_emit",
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "plan_review_emit: update_message failed for %s",
                plan_task_id, exc_info=True,
            )
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    return inbox_task_id


def maybe_emit_plan_review_on_task_done(
    svc: Any,
    task_id: str,
    actor: str,
) -> str | None:
    """Transition-hook entry point — emit if the just-done task is plan-shaped.

    Called from :func:`pollypm.work.service_transitions.on_task_done`
    after the existing milestone-flush + task-shipped hooks. No-op for
    plan_project / critique_flow tasks (their reflection nodes own the
    canonical emit) and for tasks that already have a plan_review row.

    Best-effort — any failure is logged and swallowed so the primary
    transition is never blocked.
    """
    try:
        task = svc.get(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "plan_review_emit: get(%s) failed: %s",
            task_id, exc, exc_info=True,
        )
        return None
    if not task_is_eligible_for_backstop(task):
        return None
    return emit_plan_review_for_task(
        svc=svc,
        task=task,
        actor=actor or "polly",
        requester="user",
    )


__all__ = [
    "PLAN_REVIEW_LABEL",
    "PLAN_SHAPED_LABELS",
    "already_has_plan_review_message",
    "emit_plan_review_for_task",
    "is_plan_shaped_task",
    "maybe_emit_plan_review_on_task_done",
    "task_is_eligible_for_backstop",
]
