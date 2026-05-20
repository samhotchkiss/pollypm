"""Backend-neutral helpers for the first-shipped milestone.

Contract:
- Inputs: a service object exposing ``get_execution(task_id)`` (the
  :class:`_HasExecutions` Protocol below), task ids, optional state
  paths, and the project root path.
- Outputs: booleans signalling whether the one-time
  ``first_shipped_at`` milestone was newly recorded, plus a typed
  read of the milestone timestamp via :func:`first_shipped_at`.
- Side effects: writes ``~/.pollypm/state.json`` to durably stamp
  the milestone, routes a :class:`SignalEnvelope` through the
  signal-routing policy, and enqueues a pinned ``first_shipped``
  message into the per-project state SQLAlchemy store.
- Invariants: ``mark_first_shipped`` is idempotent — the
  ``first_shipped_at`` key is only written if absent;
  ``maybe_record_first_shipped`` skips both the state-file write and
  the activity emission when the task hasn't landed a commit
  artifact.
- Allowed dependencies: ``pollypm.atomic_io``, ``pollypm.signal_routing``,
  ``pollypm.inbox.kind``, ``pollypm.work.models`` (``ArtifactKind``),
  and lazy ``pollypm.store`` for the SQLAlchemy enqueue. No SQLite
  driver imports; no dependency on ``SQLiteWorkService``.
- Private: the leading-underscore activity helper is internal to the
  ``maybe_record_first_shipped`` orchestration and should not be
  called from outside this module.

This module was extracted from ``pollypm.work.sqlite_service`` so the
``pg_service`` cutover (#1737) doesn't depend on a sqlite-only module
to record the first-shipped milestone. Both backends import these
helpers directly from this leaf module.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pollypm.atomic_io import atomic_write_json
from pollypm.inbox.kind import InboxItemKind
from pollypm.signal_routing import (
    SignalActionability,
    SignalAudience,
    SignalEnvelope,
    SignalSeverity,
    compute_dedupe_key,
    route_signal,
)
from pollypm.work.models import ArtifactKind
from pollypm.config import GLOBAL_CONFIG_DIR

logger = logging.getLogger(__name__)


_STATE_FILENAME = "state.json"


class _HasExecutions(Protocol):
    def get_execution(
        self,
        task_id: str,
        node_id: str | None = None,
        visit: int | None = None,
    ) -> list[Any]:
        ...


def state_path() -> Path:
    return GLOBAL_CONFIG_DIR / _STATE_FILENAME


def load_state(path: Path | None = None) -> dict[str, Any]:
    resolved = path or state_path()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def first_shipped_at(path: Path | None = None) -> str | None:
    value = load_state(path).get("first_shipped_at")
    return str(value) if isinstance(value, str) and value else None


def mark_first_shipped(
    *,
    path: Path | None = None,
    when: datetime | None = None,
) -> bool:
    resolved = path or state_path()
    state = load_state(resolved)
    if isinstance(state.get("first_shipped_at"), str) and state["first_shipped_at"]:
        return False
    state["first_shipped_at"] = (when or datetime.now(UTC)).isoformat()
    atomic_write_json(resolved, state)
    return True


def _record_first_shipped_activity(
    *,
    project_path: Path | None,
    project_key: str | None,
    when: datetime | None = None,
) -> None:
    """Persist the one-time shipment milestone into the project feed."""
    if project_path is None:
        return
    try:
        from pollypm.config import load_config
        from pollypm.store import get_store
    except Exception:  # noqa: BLE001
        # #1355: previously silent. A failed store import drops the
        # first-shipment celebration without a trace; log so a broken
        # store package stops masking the milestone.
        logger.warning(
            "first_shipped: store import failed for %s",
            project_key,
            exc_info=True,
        )
        return

    # Typed helper routes through the doubled-pollypm-path guard (#1972).
    from pollypm.projects import project_state_db_path

    state_db = project_state_db_path(project_path)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    shipped_at = (when or datetime.now(UTC)).isoformat()
    body = json.dumps(
        {
            "summary": "First PR shipped with Polly 🎉",
            "severity": "routine",
            "verb": "celebrated",
            "subject": "first shipment",
            "project": project_key,
            "shipped_at": shipped_at,
        }
    )
    payload = {
        "kind": "first_shipped",
        "project": project_key,
        "pinned": True,
        "shipped_at": shipped_at,
    }
    # #894 — route through SignalEnvelope before the legacy
    # store.enqueue_message write. first_shipped is informational —
    # it lands on Activity + Inbox without toasting (the user
    # discovers the celebration in their feed).
    route_signal(
        SignalEnvelope(
            audience=SignalAudience.USER,
            severity=SignalSeverity.INFO,
            actionability=SignalActionability.INFORMATIONAL,
            source="work_service",
            subject="First PR shipped",
            body="First PR shipped with Polly 🎉",
            project=project_key,
            dedupe_key=compute_dedupe_key(
                source="work_service",
                kind="first_shipped",
                target=project_key,
            ),
            payload=payload,
        )
    )
    # Post-sqlite-ripout (refs #1971): pg-backed singleton from
    # ``get_store(load_config())``. Do NOT close — it's process-wide.
    try:
        store = get_store(load_config())
    except Exception:  # noqa: BLE001
        logger.warning(
            "first_shipped: get_store(load_config()) failed for %s",
            project_key,
            exc_info=True,
        )
        return
    store.enqueue_message(
        type="event",
        tier="immediate",
        recipient="*",
        sender="polly",
        subject="first_shipped",
        body=body,
        scope="polly",
        payload=payload,
        kind=InboxItemKind.COMPLETION_FYI.value,
    )


def task_landed_commit(service: _HasExecutions, task_id: str) -> bool:
    try:
        executions = service.get_execution(task_id)
    except Exception:  # noqa: BLE001
        # #1355: previously silent. A broken get_execution call
        # forces the "first PR shipped" celebration to skip; log
        # so we stop swallowing the underlying query failure.
        logger.warning(
            "task_landed_commit: get_execution failed for %s",
            task_id,
            exc_info=True,
        )
        return False
    for execution in reversed(executions):
        work_output = getattr(execution, "work_output", None)
        if work_output is None:
            continue
        artifacts = getattr(work_output, "artifacts", None) or []
        for artifact in artifacts:
            if getattr(artifact, "kind", None) == ArtifactKind.COMMIT:
                return True
    return False


def maybe_record_first_shipped(
    service: _HasExecutions,
    task_id: str,
    *,
    path: Path | None = None,
    project_path: Path | None = None,
    when: datetime | None = None,
) -> bool:
    if not task_landed_commit(service, task_id):
        return False
    created = mark_first_shipped(path=path, when=when)
    if not created:
        return False
    project_key = task_id.split("/", 1)[0] if "/" in task_id else None
    _record_first_shipped_activity(
        project_path=project_path,
        project_key=project_key,
        when=when,
    )
    return True
