"""Read-side adapters between the work-service and Web API models.

The Web API never reaches into ``state.db`` or ``audit.jsonl`` directly
— it composes against :func:`pollypm.work.factory.create_work_service`
(#1389) and :mod:`pollypm.audit.log`. This module owns the conversions
from those internal types to the Pydantic shapes declared in
:mod:`pollypm.web_api.models`.

Phase 1 was read-only; Phase 2 (#1548) layers in write helpers
(``queue_task`` is the first wedge) that go through the same factory
so there is exactly one writer surface.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite3

import psycopg
import psycopg_pool

from pollypm.audit.log import AuditEvent, read_events
from pollypm.config import PollyPMConfig, load_config
from pollypm.models import KnownProject
from pollypm.work.inbox_view import is_inbox_task
from pollypm.web_api.errors import (
    APIError,
    not_found,
    service_unavailable,
)
from pollypm.web_api.models import (
    ContextEntry as APIContextEntry,
    Event as APIEvent,
    FlowNodeExecution as APIFlowNodeExecution,
    InboxItem as APIInboxItem,
    InboxItemDetail as APIInboxItemDetail,
    InboxMessage as APIInboxMessage,
    Plan as APIPlan,
    PlanJudgmentCall as APIPlanJudgmentCall,
    Project as APIProject,
    ProjectActivityEntry as APIProjectActivityEntry,
    ProjectDrilldown as APIProjectDrilldown,
    TaskDetail as APITaskDetail,
    TaskRelationships as APITaskRelationships,
    TaskSummary as APITaskSummary,
    Transition as APITransition,
    WorkOutput as APIWorkOutput,
    Artifact as APIArtifact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only work-service open
# ---------------------------------------------------------------------------


_DISABLE_WORK_DB_OPENED_AUDIT_ENV = "POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT"


# Known transient backing-store error classes. Failures of these
# types map to a typed 503 ``service_unavailable`` so the client can
# retry. Anything outside this tuple bubbles up to the FastAPI
# unhandled-exception handler (500 ``internal_error``) so we don't
# silently swallow real bugs.
#
# Pg-only backend (``pollypm.work.factory`` post #1971): a real pg
# outage / pool exhaustion raises ``psycopg.OperationalError`` /
# ``psycopg_pool.PoolTimeout``, neither of which subclasses
# ``OSError`` — without them in the tuple the documented 503
# envelope is bypassed and the new inbox write endpoints return 500
# on a real outage. Codex round-6 blocker 1 on PR #2060.
# ``sqlite3.*`` entries are kept for the legacy sqlite-flavoured
# integration tests that still construct fakes raising those types;
# the production path will never see them.
_BACKING_STORE_ERRORS: tuple[type[BaseException], ...] = (
    sqlite3.OperationalError,
    sqlite3.DatabaseError,
    OSError,
    psycopg.OperationalError,
    psycopg_pool.PoolTimeout,
)


@contextlib.contextmanager
def _open_work_service_readonly(
    *, config: PollyPMConfig, project_key: str, project_path: Path | str
):
    """Open a work-service for read-only API consumption.

    SQLiteWorkService doesn't yet have a true ``mode=ro`` URI flag —
    its constructor calls :func:`create_work_tables` and emits a
    ``work_db.opened`` audit row, both of which technically mutate the
    backing store. For the Web API's read endpoints we don't want
    every ``GET`` to write an audit row, so we toggle the existing
    ``POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT`` opt-out env (introduced
    upstream for tests) for the lifetime of the open.

    The ``CREATE TABLE IF NOT EXISTS`` calls in the constructor stay
    no-ops once the tables exist; we accept the first-time bootstrap
    side effect because (a) the cockpit normally bootstraps before the
    API server runs, and (b) without it a fresh workspace would 500
    on every endpoint until something else opened the DB. If/when the
    work service grows a real read-only URI flag this helper should
    forward it; for now the audit-suppression is the only meaningful
    side effect we can avoid.
    """
    from pollypm.work.factory import create_work_service

    prior = os.environ.get(_DISABLE_WORK_DB_OPENED_AUDIT_ENV)
    os.environ[_DISABLE_WORK_DB_OPENED_AUDIT_ENV] = "1"
    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project_path
        ) as svc:
            yield svc
    finally:
        if prior is None:
            os.environ.pop(_DISABLE_WORK_DB_OPENED_AUDIT_ENV, None)
        else:
            os.environ[_DISABLE_WORK_DB_OPENED_AUDIT_ENV] = prior


# ---------------------------------------------------------------------------
# Public chat-surface helpers (used by routes/chat_messages.py)
# ---------------------------------------------------------------------------


class WorkServiceFacadeUnavailable(RuntimeError):
    """Raised by :func:`list_active_worker_sessions_strict` on facade outage.

    Distinct from "no active workers" (which collapses to ``[]``):
    lets callers distinguish a genuinely empty per-task worker registry
    from a pg-pool outage / failed work-service open. Callers that need
    to map facade failures to a typed 503 ``service_unavailable``
    (instead of swallowing them like the fail-soft sibling) import
    this exception and the strict variant together.
    """


def list_active_worker_sessions_strict(config: PollyPMConfig) -> list[Any]:
    """Strict variant of :func:`list_active_worker_sessions`.

    Same return shape, but raises :class:`WorkServiceFacadeUnavailable`
    when the work-service can't be opened or
    ``list_worker_sessions(active_only=True)`` raises — instead of
    swallowing those errors and returning ``[]``. The chat-messages
    route uses this so a pg-pool outage on a worker lookup surfaces as
    a typed 503 ``service_unavailable`` instead of a misleading 404
    ``session_unknown`` (round-6 blocker).

    "No active workers" still collapses to ``[]`` (it's not an
    outage), and "no default project configured" likewise returns
    ``[]`` — there can't be any per-task workers without a project,
    so the caller treats that as a legitimate empty registry.
    """
    project = getattr(config, "project", None)
    if project is None:
        return []
    project_key = getattr(project, "name", "")
    project_path = getattr(project, "root_dir", None)
    if not project_key or project_path is None:
        return []
    try:
        with _open_work_service_readonly(
            config=config,
            project_key=project_key,
            project_path=project_path,
        ) as svc:
            list_fn = getattr(svc, "list_worker_sessions", None)
            if not callable(list_fn):
                return []
            records = list_fn(active_only=True)
            return list(records or [])
    except Exception as exc:  # noqa: BLE001
        raise WorkServiceFacadeUnavailable(str(exc)) from exc


def list_active_worker_sessions(config: PollyPMConfig) -> list[Any]:
    """Return active ``WorkerSessionRecord``s for chat surface discovery.

    Public facade so the chat-messages route doesn't need to reach
    into ``_open_work_service_readonly``. Returns ``[]`` when the
    work-service can't be opened (no DB yet, pg pool down, no default
    project configured) — the chat-surface enumerator treats an empty
    list as "no per-task workers right now" and the discovery endpoint
    still returns configured surfaces. Matches the fail-open posture
    of the other read endpoints.

    The records are the same ``WorkerSessionRecord`` type returned by
    :meth:`pollypm.work.service.WorkService.list_worker_sessions`; we
    type the return as ``list[Any]`` because importing the dataclass
    here would pull the entire work-package into the web-api service
    module at import time (and the consumer only needs duck-typed
    attribute access).
    """
    project = getattr(config, "project", None)
    if project is None:
        return []
    project_key = getattr(project, "name", "")
    project_path = getattr(project, "root_dir", None)
    if not project_key or project_path is None:
        return []
    try:
        with _open_work_service_readonly(
            config=config,
            project_key=project_key,
            project_path=project_path,
        ) as svc:
            list_fn = getattr(svc, "list_worker_sessions", None)
            if not callable(list_fn):
                return []
            try:
                records = list_fn(active_only=True)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "list_active_worker_sessions: list_worker_sessions failed",
                    exc_info=True,
                )
                return []
            return list(records or [])
    except Exception:  # noqa: BLE001
        logger.debug(
            "list_active_worker_sessions: work-service open failed",
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------


def list_projects(config: PollyPMConfig, *, tracked_only: bool = False) -> list[APIProject]:
    """Return every registered project as an :class:`APIProject`.

    Counts and flags are computed against the work-service so the
    response matches what the cockpit dashboard renders. We open one
    work-service per project to keep the implementation simple — the
    factory is cheap and the cockpit does the same.
    """
    out: list[APIProject] = []
    for key, project in config.projects.items():
        if tracked_only and not project.tracked:
            continue
        out.append(_project_to_api(config, key, project))
    return out


def get_project(config: PollyPMConfig, key: str) -> APIProject | None:
    project = config.projects.get(key)
    if project is None:
        return None
    return _project_to_api(config, key, project)


def project_drilldown(config: PollyPMConfig, key: str) -> APIProjectDrilldown | None:
    """Project + recent activity + top tasks + pending plan review.

    One round-trip is enough to render the cockpit's drilldown per
    spec §8 (``GET /api/v1/projects/{key}``).
    """
    base = get_project(config, key)
    if base is None:
        return None
    project_path = config.projects[key].path

    recent: list[APIProjectActivityEntry] = []
    try:
        events = read_events(key, project_path=project_path, limit=25)
    except Exception:  # noqa: BLE001
        events = []
    for event in events:
        try:
            ts = _parse_iso(event.ts)
        except Exception:  # noqa: BLE001
            continue
        if ts is None:
            continue
        meta = event.metadata or {}
        summary = meta.get("summary") or meta.get("message")
        recent.append(APIProjectActivityEntry(
            ts=ts,
            event=event.event,
            subject=event.subject,
            actor=event.actor or "",
            status=event.status,
            summary=str(summary) if summary else None,
        ))

    top_tasks: list[APITaskSummary] = []
    plan: APIPlan | None = None
    try:
        with _open_work_service_readonly(
            config=config, project_key=key, project_path=project_path
        ) as svc:
            tasks = svc.list_tasks(project=key, limit=10)
            for task in tasks:
                top_tasks.append(_task_to_summary(task))
            plan = _active_plan_for_project(svc, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drilldown: work-service open failed for %s: %s", key, exc)

    return APIProjectDrilldown(
        **base.model_dump(),
        recent_activity=recent,
        top_tasks=top_tasks,
        plan_review=plan,
    )


# ---------------------------------------------------------------------------
# Project write helpers (Phase 2 — projects pause/resume/archive/init-guide)
# ---------------------------------------------------------------------------


def _resolve_config_write_path(config: PollyPMConfig) -> Path:
    """Pick the TOML path mutations should write back to.

    Prefers ``config.config_path`` (stamped by :func:`load_config` post
    PR #2026) and falls back to ``DEFAULT_CONFIG_PATH``. Tests that
    construct a :class:`PollyPMConfig` by hand must set
    ``config_path`` to a tmp file or the helper writes to the user's
    real ``~/.pollypm/pollypm.toml`` — same constraint as
    ``pollypm.projects.enable_tracked_project``.
    """
    if config.config_path is not None:
        return Path(config.config_path)
    from pollypm.config import DEFAULT_CONFIG_PATH

    return Path(DEFAULT_CONFIG_PATH)


def _emit_project_audit(
    *,
    event: str,
    config: PollyPMConfig,
    project_key: str,
    project_path: Path | None,
    actor: str,
    reason: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit emit for project lifecycle mutations.

    Mirrors :func:`pollypm.cockpit_pane_reaper._emit_audit`: a failure
    in the audit subsystem must NOT block (or roll back) the API
    mutation, so this swallows every exception. ``reason`` is bounded
    at 500 chars upstream by ``_ReasonBody`` but we clamp again here
    as belt-and-suspenders in case a future caller bypasses the
    Pydantic model.
    """
    try:
        from pollypm.audit import emit as _audit_emit
    except Exception:  # noqa: BLE001
        return
    metadata: dict[str, Any] = {"source": "web_api"}
    if reason is not None:
        metadata["reason"] = reason[:500]
    if extra:
        metadata.update(extra)
    try:
        _audit_emit(
            event=event,
            project=project_key,
            subject=f"projects/{project_key}",
            actor=actor or "api",
            status="ok",
            metadata=metadata,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        pass


def set_project_tracked(
    config: PollyPMConfig,
    project_key: str,
    *,
    tracked: bool,
    reason: str | None = None,
    actor: str = "api",
) -> APIProject:
    """Flip ``KnownProject.tracked`` and re-render the global TOML.

    Concurrent-safe write semantics (Codex review on #2063):

    * Re-load the on-disk TOML so any external CLI / cockpit edits
      that landed between server boot and this call are preserved —
      we never write back the long-lived ``ConfigDep`` snapshot with
      ``force=True`` (that would silently drop e.g. a project added
      via ``pm add-project`` after the server started).
    * Mutate the freshly-loaded copy, NOT the live ``config``.
    * Only after the disk write succeeds do we sync the change back
      into the live ``config`` so subsequent in-process reads see it.
    * If ``write_config`` raises ``OSError`` the live ``config``
      stays untouched and a subsequent GET reflects the unchanged
      disk state — no stale in-memory lie that flips back on restart.

    Returns the post-mutation :class:`APIProject` snapshot so the
    client can refresh without a follow-up GET (same idiom as the
    task transitions in Phase 2).
    """
    from pollypm.config import load_config, write_config

    # Bind to the original (in-memory) project up-front so the 404 path
    # doesn't pay for a disk reload.
    live_project = config.projects.get(project_key)
    if live_project is None:
        raise not_found(f"Project not registered: {project_key}")

    config_path = _resolve_config_write_path(config)
    # Reload from disk so we don't lose concurrent CLI / cockpit edits
    # (e.g. an external ``pm add-project`` that added a new project key
    # the in-memory server hasn't seen yet).
    try:
        fresh = load_config(config_path)
    except OSError as exc:
        raise service_unavailable(
            f"Failed to reload config for {project_key}: {exc}",
            hint="Check read permissions on the PollyPM config file.",
        ) from exc
    fresh_project = fresh.projects.get(project_key)
    if fresh_project is None:
        # Disk-side delete raced us. Treat as 404 — the in-memory state
        # is stale and the next GET will agree.
        raise not_found(f"Project not registered: {project_key}")

    fresh_project.tracked = tracked
    fresh.projects[project_key] = fresh_project
    try:
        write_config(fresh, config_path, force=True)
    except OSError as exc:
        # Live config untouched — Codex P0 #1 (rollback guarantee).
        raise service_unavailable(
            f"Failed to persist project state for {project_key}: {exc}",
            hint="Check write permissions on the PollyPM config file.",
        ) from exc

    # Disk write succeeded — now sync the live config so in-process
    # callers see the new state without waiting for a load_config()
    # cache refresh. We update the existing KnownProject in place so
    # any objects holding a reference to it observe the new value.
    live_project.tracked = tracked
    config.projects[project_key] = live_project
    # Merge any external project additions that landed on disk so a
    # subsequent in-process GET sees them too (defence-in-depth — the
    # next load_config() will rediscover them anyway via mtime).
    for key, value in fresh.projects.items():
        if key not in config.projects:
            config.projects[key] = value

    _emit_project_audit(
        event="projects.tracked.set",
        config=config,
        project_key=project_key,
        project_path=live_project.path,
        actor=actor,
        reason=reason,
        extra={"tracked": tracked},
    )
    return _project_to_api(config, project_key, live_project)


def archive_project(
    config: PollyPMConfig,
    project_key: str,
    *,
    reason: str | None = None,
    actor: str = "api",
) -> str:
    """Remove ``project_key`` from ``config.projects`` and persist.

    Per spec §6.2 "Archive ... Removes from config; Source data on
    disk untouched." Returns the removed project's display label so
    the route can populate an :class:`ActionResult` message.

    Archive is irreversible from the API: subsequent ``resume`` /
    ``pause`` / ``init-guide`` calls 404 because the project key is
    gone. Re-registering uses ``pm add-project`` (CLI-only).

    Codex review on #2063 (P0 #3): routes through
    :func:`pollypm.projects.remove_project` so the session→project
    invariant (no enabled session may reference a missing project) is
    enforced uniformly with the CLI's ``pm projects remove``. Enabled-
    session references map to ``409 conflict`` with the blocking
    session names in the body.
    """
    import typer

    from pollypm.projects import remove_project as _remove_project_facade

    live_project = config.projects.get(project_key)
    if live_project is None:
        raise not_found(f"Project not registered: {project_key}")

    label = live_project.display_label()
    project_path = live_project.path
    config_path = _resolve_config_write_path(config)
    try:
        # ``remove_project`` reloads from disk, enforces the session-ref
        # guard, then writes back — same concurrent-safe pattern as the
        # tracked-toggle path above. Live ``config`` is untouched if the
        # facade raises.
        _remove_project_facade(config_path, project_key)
    except typer.BadParameter as exc:
        msg = str(exc)
        if "still used by" in msg:
            raise APIError(
                status_code=409,
                code="conflict",
                message=msg,
                hint=(
                    "Disable or remove the listed sessions before "
                    "archiving this project."
                ),
            ) from exc
        # Disk-side race (project gone between our 404 check and the
        # facade's reload). Treat as 404 so the client sees a stable
        # error code.
        raise not_found(msg) from exc
    except OSError as exc:
        raise service_unavailable(
            f"Failed to persist archive for {project_key}: {exc}",
            hint="Check write permissions on the PollyPM config file.",
        ) from exc

    # Disk write succeeded — sync the live config.
    config.projects.pop(project_key, None)

    _emit_project_audit(
        event="projects.archive",
        config=config,
        project_key=project_key,
        project_path=project_path,
        actor=actor,
        reason=reason,
        extra={"label": label},
    )
    return label


def init_project_guide_for_role(
    config: PollyPMConfig,
    project_key: str,
    *,
    role: str,
    force: bool = False,
) -> dict[str, Any]:
    """Wrap :func:`pollypm.project_guides.init_project_guide`.

    Returns ``{role, path, forked_from, body}`` so clients can preview
    the seeded markdown without a follow-up GET. Role validation maps
    to ``422 validation_error`` (matches the spec §6 matrix —
    unsupported enum value); existing-without-force maps to ``409
    conflict`` mirroring the cockpit's ``--force`` UX.
    """
    from pollypm.project_guides import init_project_guide

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    try:
        info = init_project_guide(project.path, role, force=force)
    except ValueError as exc:
        # ``validate_project_guide_role`` raises ``ValueError`` for
        # unknown roles. Spec §6 maps that to 422 (body validates as
        # JSON but the enum value is wrong).
        raise APIError(
            status_code=422,
            code="validation_error",
            message=str(exc),
            hint="Supported roles: architect, reviewer, worker.",
        ) from exc
    except FileExistsError as exc:
        raise APIError(
            status_code=409,
            code="conflict",
            message=str(exc),
            hint="Re-send with `force=true` to overwrite the existing guide.",
        ) from exc
    except OSError as exc:
        raise service_unavailable(
            f"Failed to write project guide for {project_key}: {exc}",
        ) from exc

    return {
        "role": info.role,
        "path": str(info.path),
        "forked_from": info.forked_from,
        "body": info.body,
    }


def _project_to_api(config: PollyPMConfig, key: str, project: KnownProject) -> APIProject:
    counts: dict[str, int] = {}
    pending_plan_review = False
    open_inbox_count = 0
    glyph = "unknown"
    state_label: str | None = None

    try:
        with _open_work_service_readonly(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            try:
                counts = svc.state_counts(project=key) or {}
            except Exception:  # noqa: BLE001
                counts = {}
            try:
                pending_plan_review = _has_pending_plan_review(svc, key)
            except Exception:  # noqa: BLE001
                pending_plan_review = False
    except Exception as exc:  # noqa: BLE001
        logger.debug("project counts: work-service unavailable for %s: %s", key, exc)

    try:
        open_inbox_count = _count_open_inbox(config, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project inbox count failed for %s: %s", key, exc)

    glyph = _glyph_for_project(project, counts, pending_plan_review, open_inbox_count)
    if not project.tracked:
        glyph = "paused"

    return APIProject(
        key=key,
        name=project.display_label(),
        path=str(project.path),
        tracked=project.tracked,
        kind=project.kind.value if hasattr(project.kind, "value") else str(project.kind),
        persona_name=project.persona_name,
        state=state_label,
        glyph=glyph,
        task_counts=counts,
        open_inbox_count=open_inbox_count,
        pending_plan_review=pending_plan_review,
    )


def _glyph_for_project(
    project: KnownProject,
    counts: dict[str, int],
    pending_plan_review: bool,
    open_inbox_count: int,
) -> str:
    """Best-effort stop-light glyph.

    Mirrors the cockpit's signaling: red when there's pending plan
    review or open inbox waiting, amber when there's review/in_progress
    work, green otherwise. Real briefing-derived glyphs land on the
    cockpit dashboard via ``dashboard_data.gather`` — that path needs a
    StateStore + plugin host, which the API server intentionally
    doesn't load. The fallback derives from raw work-service counts so
    it works with the cockpit down.
    """
    if not project.tracked:
        return "paused"
    if pending_plan_review or open_inbox_count > 0:
        return "amber"
    if counts.get("review", 0) > 0 or counts.get("blocked", 0) > 0:
        return "amber"
    if counts.get("in_progress", 0) > 0:
        return "amber"
    return "green"


def _has_pending_plan_review(svc, project_key: str) -> bool:
    """True when any task is sitting at ``review`` on a plan-review node.

    Phase 1 only needs a boolean, so we read tasks-in-review and check
    whether their flow template is plan-shaped.
    """
    try:
        tasks = svc.list_tasks(project=project_key, work_status="review")
    except Exception:  # noqa: BLE001
        return False
    for task in tasks:
        flow_id = getattr(task, "flow_template_id", "") or ""
        if "plan" in flow_id.lower():
            return True
        labels = getattr(task, "labels", []) or []
        if any("plan" in lbl.lower() for lbl in labels):
            return True
    return False


def _count_open_inbox(config: PollyPMConfig, project_key: str) -> int:
    """Count open inbox messages + chat-flow tasks for a project.

    Mirrors :func:`pollypm.dashboard_data._count_inbox_tasks` at a
    project granularity. Best-effort — returns 0 on any failure
    rather than blocking the project list.
    """
    project = config.projects.get(project_key)
    if project is None:
        return 0
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            tasks = svc.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    for task in tasks:
        if getattr(task, "flow_template_id", "") != "chat":
            continue
        status = getattr(task, "work_status", None)
        status_value = getattr(status, "value", str(status)) if status else ""
        if status_value not in {"done", "cancelled"}:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------


def list_project_tasks(
    config: PollyPMConfig,
    project_key: str,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[APITaskSummary], str | None]:
    """Page of task summaries with cursor-based pagination.

    The cursor is the integer ``task_number`` of the last item in the
    previous page; absent ⇒ start. We sort tasks by ``task_number`` so
    the cursor is stable across calls without depending on
    ``updated_at``.
    """
    project = config.projects.get(project_key)
    if project is None:
        return [], None

    cursor_n: int | None = None
    if cursor is not None:
        try:
            cursor_n = int(cursor)
        except ValueError:
            cursor_n = None

    # Wrap the entire ``with`` so failures during work-service
    # construction (DB open, pragmas, schema bootstrap, migrations)
    # also surface as 503 — not just failures inside the body.
    # Spec §6 maps DB lock contention / I/O failures to
    # ``service_unavailable`` so the client can retry. APIError /
    # other typed exceptions fall outside ``_BACKING_STORE_ERRORS``
    # so they pass through unchanged.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            tasks = svc.list_tasks(project=project_key, work_status=status)
            tasks.sort(key=lambda t: getattr(t, "task_number", 0))
            if cursor_n is not None:
                tasks = [t for t in tasks if getattr(t, "task_number", 0) > cursor_n]
            page = tasks[:limit]
            next_cursor: str | None = None
            if len(tasks) > limit and page:
                next_cursor = str(getattr(page[-1], "task_number", 0))
            return [_task_to_summary(t) for t in page], next_cursor
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "list_tasks: backing store error for %s: %s",
            project_key,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for project {project_key}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def get_task_detail(
    config: PollyPMConfig, project_key: str, task_number: int
) -> APITaskDetail | None:
    project = config.projects.get(project_key)
    if project is None:
        return None

    task_id = f"{project_key}/{task_number}"
    # Wrap the entire ``with`` so failures during work-service
    # construction (DB open, pragmas, schema bootstrap, migrations)
    # surface as 503, not 500. Genuine missing-task failures (the
    # narrow ``Exception`` swallow inside ``svc.get(...)``) still
    # collapse to ``None`` ⇒ 404 — but a backing-store failure on
    # ``svc.get`` re-raises ``OperationalError`` past the inner
    # swallow so the outer handler can map it to 503.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                task = svc.get(task_id)
            except _BACKING_STORE_ERRORS:
                # Re-raise so the outer handler converts to 503 —
                # don't conflate a DB error with "task not found".
                raise
            except Exception:  # noqa: BLE001
                return None
            plan: APIPlan | None = None
            if _is_plan_task(task) and _is_in_review(task):
                try:
                    plan = _build_plan(svc, task)
                except Exception:  # noqa: BLE001
                    plan = None
            return _task_to_detail(task, plan=plan)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "get_task_detail: backing store error for %s/%s: %s",
            project_key,
            task_number,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for task {project_key}/{task_number}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


# ---------------------------------------------------------------------------
# Write helpers (Phase 2)
#
# Each helper opens a fresh work-service via :func:`create_work_service`
# — the same canonical writer the cockpit uses (#1389). The Web API is
# never a second writer surface; it's a thin adapter that translates
# work-service exceptions into the API's typed error envelope (§6).
# ---------------------------------------------------------------------------


def queue_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str = "api",
) -> APITaskDetail:
    """Transition a draft task to ``queued`` via the work-service.

    Mirrors ``pm work queue`` (`pollypm.work.cli.queue_cmd`) so the
    cockpit and the API share the same state machine. Returns the
    updated :class:`TaskDetail` so the client can refresh its UI
    without a follow-up ``GET``.

    Errors map onto the spec §6 codes:

    * Project not registered → ``not_found`` (404)
    * Task not found in DB → ``not_found`` (404)
    * Task not in ``draft`` (or transition rejected by the state
      machine for any other reason) → ``conflict`` (409,
      ``invalid_state``)
    * Backing-store unavailable → ``service_unavailable`` (503)
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
        ValidationError as WorkValidationError,
    )

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    task_id = f"{project_key}/{task_number}"
    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                svc.queue(task_id, actor)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                # Issue #1548 tests call for "Queue a non-draft task →
                # 409 conflict with invalid_state": HTTP 409 (the verb
                # cockpit users associate with "the state changed
                # underneath you") plus the stable ``invalid_state``
                # code from spec §6 so clients can route on it. The
                # state machine's message is the most informative
                # thing to surface; clients can show it verbatim.
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=(
                        str(exc)
                        or f"Task {task_id} cannot be queued from its current state."
                    ),
                    hint="Only draft tasks can be queued; refresh the task to see the current work_status.",
                ) from exc
            except WorkValidationError as exc:
                # Gate failures (e.g. ``has_description``) raise
                # ``ValidationError`` from the work service. Spec §6
                # maps that to 422 ``validation_error`` — the body
                # shape was fine, but the underlying task's data
                # failed validation. We surface the gate's reason
                # verbatim so clients can show it (cockpit does the
                # same with ``--skip-gates``-style overrides).
                raise APIError(
                    status_code=422,
                    code="validation_error",
                    message=str(exc) or f"Task {task_id} failed pre-queue gates.",
                    hint="Fix the failing gate (e.g. add a description) before queueing.",
                ) from exc
            # Re-read so the response carries the post-transition
            # snapshot the client would see on a follow-up GET.
            task = svc.get(task_id)
            plan: APIPlan | None = None
            if _is_plan_task(task) and _is_in_review(task):
                try:
                    plan = _build_plan(svc, task)
                except Exception:  # noqa: BLE001
                    plan = None
            return _task_to_detail(task, plan=plan)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "queue_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while queueing {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


# ---------------------------------------------------------------------------
# Inbox write helpers (Phase 2 — spec §4.1)
#
# Each helper opens a fresh work-service via :func:`create_work_service`
# — the same canonical writer the cockpit uses. The Web API is never a
# second writer surface; it's a thin adapter that maps inbox-item ids
# (``project/n``) onto the existing work-service inbox methods and
# translates the resulting exceptions into typed error envelopes (§6).
#
# Why these live next to ``queue_task`` and not under
# ``pollypm.work.inbox_cli``: the CLI helpers all assume a Typer call
# graph (they ``raise typer.Exit`` on failure and write to stdout for
# bulk modes). Routing an HTTP request through Typer is the wrong
# shape; the helpers below call the same underlying ``PgWorkService``
# methods (``archive_task``, ``add_reply``, ``mark_read``,
# ``add_context``, ``create``) the CLI invokes.
# ---------------------------------------------------------------------------


# Statuses we treat as "already archived" for the inbox archive
# endpoint. ``archive_task`` itself is idempotent (returns the row
# unchanged when terminal) — but the spec wants a typed 409 so the
# client can tell "I just archived it" from "someone else already
# did". We pre-check the status and surface the conflict instead of
# silently no-op'ing.
_ALREADY_ARCHIVED_STATUSES: frozenset[str] = frozenset({"done", "cancelled"})


def _project_key_from_inbox_id(item_id: str) -> str:
    """Pull the project key off an inbox item id (``project/n``).

    Inbox ids use the same ``project/task_number`` shape task ids use
    (see :func:`_task_to_inbox_item`); the first segment is always the
    project key. We accept ``msg:<n>`` here too so a future router
    that wants to route those onto the unified messages store can
    branch on the prefix — for now ``msg:`` ids raise ``not_found``
    because the Web API only addresses chat-flow tasks.
    """
    if "/" not in item_id:
        raise not_found(
            f"Inbox item not found: {item_id}",
            hint=(
                "Inbox ids look like 'project/n'. Message-store ids "
                "(``msg:<n>``) are not yet supported on this surface."
            ),
        )
    return item_id.split("/", 1)[0]


def _resolve_inbox_project(
    config: PollyPMConfig, item_id: str
) -> tuple[str, KnownProject]:
    """Return ``(project_key, project)`` for an inbox id, or 404.

    Centralizes the "id → registered project" lookup so every inbox
    write endpoint reports the same error shape on unknown ids.
    """
    key = _project_key_from_inbox_id(item_id)
    project = config.projects.get(key)
    if project is None:
        raise not_found(f"Project not registered: {key}")
    return key, project


def archive_inbox_item(
    config: PollyPMConfig,
    item_id: str,
    *,
    reason: str | None = None,
    actor: str = "api",
) -> APITaskDetail:
    """Archive an inbox item via ``svc.archive_task``.

    Returns the post-transition :class:`TaskDetail` so the client can
    re-render without a follow-up GET. Returns 409 ``invalid_state``
    when the item is already terminal (mirrors the spec §4.3 contract:
    "archive a resolved item → invalid_state").
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
    )

    key, project = _resolve_inbox_project(config, item_id)
    try:
        with create_work_service(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            # Quick existence/auth probe so unknown ids surface as 404
            # before we attempt the (atomic) transition. The terminal
            # check is intentionally NOT here: ``archive_task`` with
            # ``strict=True`` performs that check atomically inside
            # the canonical transition, so two concurrent archivers
            # see exactly one 200 and one 409 (closes the race the
            # earlier pre-check version exposed — #2060).
            try:
                src_task = svc.get(item_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            # Membership guard (#2060 round-3/round-5): the bare
            # ``svc.get`` above returns ANY task with that id,
            # including non-inbox work rows that the GET /inbox
            # surface would never expose. We route resolution through
            # the canonical :func:`pollypm.work.inbox_view.is_inbox_task`
            # used by cockpit / rail / dashboard so the write surface
            # cannot drift open relative to the read surface (Codex
            # round-5 blocker on #2060).
            if not is_inbox_task(src_task, svc):
                raise not_found(f"Inbox item not found: {item_id}")
            # Run the strict transition FIRST so the reason note is
            # only persisted on a successful archive (#2060 round-4
            # blocker 2). The earlier ordering wrote the note before
            # the transition; a losing concurrent archiver returned
            # 409 with the reason already attached to a task that
            # this caller had not, in fact, archived — leaving stray
            # ``archive reason:`` notes on terminal items and a
            # confusing audit trail.
            try:
                svc.archive_task(item_id, actor=actor, strict=True)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            except InvalidTransitionError as exc:
                # The atomic UPDATE asserted the row was non-terminal;
                # losing the race means another caller archived first.
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc) or (
                        f"Inbox item {item_id} is already terminal."
                    ),
                    hint="Items in a terminal state cannot be re-archived.",
                ) from exc
            if reason:
                # Record the operator-supplied reason AFTER the strict
                # transition succeeds so failed archives leave no
                # note. Best-effort: a failed context-write must not
                # roll back the (already-committed) archive — the
                # transition itself is the source of truth, the note
                # is supplementary audit context.
                try:
                    svc.add_context(
                        item_id, actor, f"archive reason: {reason}",
                        entry_type="note",
                    )
                except Exception:  # noqa: BLE001 — non-fatal context-write
                    logger.debug(
                        "archive_inbox_item: reason note failed for %s",
                        item_id, exc_info=True,
                    )
            task = svc.get(item_id)
            return _task_to_detail(task)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "archive_inbox_item: backing store error for %s: %s",
            item_id, exc, exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while archiving {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def snooze_inbox_item(
    config: PollyPMConfig,
    item_id: str,
    *,
    duration_seconds: int | None = None,
    until: datetime | None = None,
    reason: str | None = None,
    actor: str = "api",
) -> APITaskDetail:
    """Snooze an inbox item until a future time.

    No native ``svc.snooze`` exists on the work-service (#1776 only
    shipped reply/mark_read/archive). We persist the snooze as a
    structured ``snooze`` context entry whose text encodes the
    wake-up time + reason; the cockpit's inbox-curation predicate
    can later read these to hide snoozed rows from the default view.

    Exactly one of ``duration_seconds`` / ``until`` must be supplied.
    """
    from datetime import timedelta, timezone
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import TaskNotFoundError

    if duration_seconds is None and until is None:
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Snooze requires duration_seconds or until.",
            hint="Pass `duration_seconds` (>=1) or an ISO-8601 `until`.",
        )
    if duration_seconds is not None and until is not None:
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Pass duration_seconds OR until, not both.",
        )
    now = datetime.now(timezone.utc)
    if until is None:
        until = now + timedelta(seconds=duration_seconds or 0)
    # Coerce to tz-aware UTC for a stable comparison.
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= now:
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Snooze until must be in the future.",
        )
    # Spec §4.3: ``until`` > 30 days out is rejected.
    if (until - now) > timedelta(days=30):
        raise APIError(
            status_code=400,
            code="invalid_request",
            message="Snooze until is more than 30 days out.",
            hint="Use a wake-time within 30 days.",
        )

    key, project = _resolve_inbox_project(config, item_id)
    try:
        with create_work_service(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            try:
                current = svc.get(item_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            # Membership guard (#2060 round-3/round-5): canonical
            # predicate (see archive_inbox_item). A caller could
            # otherwise snooze a non-inbox work task that GET /inbox
            # would 404.
            if not is_inbox_task(current, svc):
                raise not_found(f"Inbox item not found: {item_id}")
            status = getattr(current.work_status, "value", str(current.work_status))
            if status in _ALREADY_ARCHIVED_STATUSES:
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=f"Inbox item {item_id} is {status}; cannot snooze.",
                )
            # Persist the wake time as a structured ``until_iso=...``
            # marker so the inbox-list predicate (_snoozed_until_for)
            # can parse it back without regex-guessing on free-form
            # text. Older entries that only carried "snoozed until
            # <iso>" still parse via the fallback path in the reader.
            payload_parts = [
                f"until_iso={until.isoformat()}",
                f"snoozed until {until.isoformat()}",
            ]
            if reason:
                payload_parts.append(f"reason: {reason}")
            try:
                svc.add_context(
                    item_id, actor, "; ".join(payload_parts),
                    entry_type="snooze",
                )
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            task = svc.get(item_id)
            return _task_to_detail(task)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "snooze_inbox_item: backing store error for %s: %s",
            item_id, exc, exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while snoozing {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def mark_read_inbox_item(
    config: PollyPMConfig,
    item_id: str,
    *,
    actor: str = "api",
) -> APITaskDetail:
    """Record a read-marker on an inbox item via ``svc.mark_read``.

    Idempotent: re-opening the same item is a no-op (the work-service
    method itself collapses repeats). Returns the current task detail
    so the client can refresh its UI without a follow-up GET.
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import TaskNotFoundError

    key, project = _resolve_inbox_project(config, item_id)
    try:
        with create_work_service(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            # Membership guard (#2060 round-3/round-5): fetch the
            # task first so a non-inbox work row can't be silently
            # mark-read'd through this endpoint. Without this,
            # ``svc.mark_read`` only checks existence and would
            # happily write a ``read`` context row against any task
            # id. Canonical inbox predicate so writes can't drift
            # past what cockpit / rail / dashboard surface.
            try:
                src_task = svc.get(item_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            if not is_inbox_task(src_task, svc):
                raise not_found(f"Inbox item not found: {item_id}")
            try:
                svc.mark_read(item_id, actor=actor)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            task = svc.get(item_id)
            return _task_to_detail(task)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "mark_read_inbox_item: backing store error for %s: %s",
            item_id, exc, exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while marking-read {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def reply_inbox_item(
    config: PollyPMConfig,
    item_id: str,
    *,
    body: str,
    owner: str | None = None,
    actor: str = "api",
) -> APITaskDetail:
    """Append a reply to an inbox thread via ``svc.add_reply``.

    The work-service strips whitespace and rejects empty bodies via
    :class:`ValidationError`; we map that to 422 so the client can
    show the underlying message.
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        TaskNotFoundError,
        ValidationError as WorkValidationError,
    )

    key, project = _resolve_inbox_project(config, item_id)
    actor_name = owner or actor or "operator"
    try:
        with create_work_service(
            config=config, project_key=key, project_path=project.path
        ) as svc:
            # Membership guard (#2060 round-3/round-5): canonical
            # inbox predicate; without this a reply to a non-inbox
            # work task would be persisted as a reply row the GET
            # /inbox surface would never expose.
            try:
                src_task = svc.get(item_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            if not is_inbox_task(src_task, svc):
                raise not_found(f"Inbox item not found: {item_id}")
            try:
                svc.add_reply(item_id, body, actor=actor_name)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            except WorkValidationError as exc:
                raise APIError(
                    status_code=422,
                    code="validation_error",
                    message=str(exc) or "Reply body failed validation.",
                ) from exc
            task = svc.get(item_id)
            return _task_to_detail(task)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "reply_inbox_item: backing store error for %s: %s",
            item_id, exc, exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while replying to {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def promote_inbox_to_task(
    config: PollyPMConfig,
    item_id: str,
    *,
    target_project: str | None = None,
    prompt: str | None = None,
    title: str | None = None,
    actor: str = "api",
) -> APITaskDetail:
    """Create a new task derived from an inbox item.

    The source item stays open (cockpit operator can archive it
    separately if they want). The new task lands in the same project
    by default — pass ``target_project`` to redirect. The new task's
    description is ``prompt`` when provided, else the source item's
    description / preview.
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import TaskNotFoundError

    src_key, src_project = _resolve_inbox_project(config, item_id)
    dest_key = target_project or src_key
    dest_project = config.projects.get(dest_key)
    if dest_project is None:
        raise not_found(f"Project not registered: {dest_key}")

    try:
        with create_work_service(
            config=config, project_key=src_key, project_path=src_project.path
        ) as src_svc:
            try:
                src_task = src_svc.get(item_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Inbox item not found: {item_id}") from exc
            # Membership guard (#2060 round-3/round-5): the GET
            # /inbox surface would 404 a non-inbox task id, so
            # promote-to-task must too — otherwise a caller can
            # derive a new task from an arbitrary work row by
            # addressing it through this verb. Canonical predicate
            # so writes don't widen past cockpit / rail / dashboard.
            if not is_inbox_task(src_task, src_svc):
                raise not_found(f"Inbox item not found: {item_id}")
            src_title = title or f"From inbox: {src_task.title}"
            src_description = (
                prompt or src_task.description or src_task.title or ""
            )
            src_priority = getattr(
                getattr(src_task, "priority", None), "value", "normal",
            )

        # Open a fresh service against the destination project so the
        # write lands on the right per-row ``project`` column even when
        # cross-project promotion is used.
        with create_work_service(
            config=config, project_key=dest_key, project_path=dest_project.path
        ) as dest_svc:
            new_task = dest_svc.create(
                title=src_title,
                description=src_description,
                type="task",
                project=dest_key,
                flow_template="standard",
                roles={"requester": actor},
                priority=src_priority or "normal",
                created_by=actor,
                labels=["promoted-from-inbox", f"source:{item_id}"],
            )
            return _task_to_detail(dest_svc.get(new_task.task_id))
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "promote_inbox_to_task: backing store error for %s: %s",
            item_id, exc, exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while promoting {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


def get_active_plan(
    config: PollyPMConfig, project_key: str, *, version: int | None = None
) -> APIPlan | None:
    """Return the structured plan body for the project's active review task.

    When ``version`` is supplied, return that revision instead of the
    current one (the work-service today only stores the latest body,
    so for now we only honor the version *number* on the active task
    — older revisions surface in Phase 2 once the architecture for
    plan history is decided).
    """
    project = config.projects.get(project_key)
    if project is None:
        return None
    # Wrap the entire ``with`` so failures during work-service
    # construction surface as 503. ``_active_plan_for_project``
    # internally swallows broad exceptions (so a transient query
    # failure during plan reconstruction degrades to "no plan"),
    # but a backing-store failure on ``__enter__`` would otherwise
    # leak as 500.
    try:
        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            plan = _active_plan_for_project(svc, project_key)
            if plan is None:
                return None
            if version is not None and plan.version != version:
                # Older versions are not retrievable yet; matching strict
                # version returns the active plan only when it matches.
                return None
            return plan
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "get_active_plan: backing store error for %s: %s",
            project_key,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for project {project_key}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def _active_plan_for_project(svc, project_key: str) -> APIPlan | None:
    try:
        review_tasks = svc.list_tasks(project=project_key, work_status="review")
    except Exception:  # noqa: BLE001
        review_tasks = []
    candidates = [t for t in review_tasks if _is_plan_task(t)]
    if not candidates:
        return None
    # Newest plan_version wins.
    candidates.sort(key=lambda t: getattr(t, "plan_version", 1) or 1, reverse=True)
    return _build_plan(svc, candidates[0])


def _build_plan(svc, task) -> APIPlan:
    body = _extract_plan_body(task)
    summary = _extract_plan_summary(body)
    judgment_calls = [
        APIPlanJudgmentCall(point=point) for point in _extract_judgment_calls(body)
    ]
    critic = _extract_critic_synthesis(body)
    created = getattr(task, "created_at", None) or datetime.utcnow()
    return APIPlan(
        task_id=task.task_id,
        version=getattr(task, "plan_version", 1) or 1,
        predecessor_task_id=getattr(task, "predecessor_task_id", None),
        summary=summary,
        judgment_calls=judgment_calls,
        body=body,
        critic_synthesis=critic,
        created_at=created,
    )


_HEADER_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")


def _extract_plan_body(task) -> str:
    """Pull the plan markdown out of the task.

    Plans land in the task's ``description`` (the architect writes the
    full markdown there before transitioning to review). We fall back
    to the latest review-node execution's ``work_output.summary`` if
    the description is empty (older flows wrote to that surface).
    """
    desc = getattr(task, "description", "") or ""
    if desc.strip():
        return desc
    executions = getattr(task, "executions", []) or []
    for execution in reversed(executions):
        wo = getattr(execution, "work_output", None)
        if wo is not None and getattr(wo, "summary", None):
            return wo.summary
    return ""


def _extract_plan_summary(body: str) -> str:
    """First non-header paragraph of the plan; mirrors cockpit logic."""
    if not body.strip():
        return ""
    lines = body.splitlines()
    # Prefer a ``## Summary`` block.
    for idx, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if match and "summary" in match.group("title").lower():
            collected: list[str] = []
            for follow in lines[idx + 1:]:
                if follow.strip().startswith("#"):
                    break
                if not follow.strip():
                    if collected:
                        break
                    continue
                collected.append(follow.strip())
            if collected:
                return " ".join(collected)
            break
    # Fall back to the first paragraph.
    collected = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if collected:
                break
            continue
        if not stripped:
            if collected:
                break
            continue
        collected.append(stripped)
    return " ".join(collected)


def _extract_judgment_calls(body: str, *, limit: int = 5) -> list[str]:
    """Mirror :func:`pollypm.cockpit_ui._extract_plan_judgment_calls`.

    The cockpit's helper isn't directly importable from the API path
    (it pulls Textual at module import time), so we reproduce its
    bullet-extraction logic here. The behaviour is identical: bullets
    under a ``## Judgment calls`` (or ``Judgement``) header, capped
    at ``limit``.
    """
    if not body.strip():
        return []
    target = {"## judgment calls", "## judgement calls", "### judgment calls"}
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower() in target:
            capturing = True
            continue
        if not capturing:
            continue
        if stripped.startswith("#"):
            break
        match = _BULLET_RE.match(line)
        if match:
            point = re.sub(r"\s+", " ", match.group("text")).strip()
            if point:
                out.append(point)
                if len(out) >= limit:
                    break
    return out


def _extract_critic_synthesis(body: str) -> str | None:
    if not body.strip():
        return None
    target = {"## critic synthesis", "### critic synthesis", "## architect critic"}
    collected: list[str] = []
    capturing = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower() in target:
            capturing = True
            continue
        if not capturing:
            continue
        if stripped.startswith("#"):
            break
        if not stripped and collected:
            collected.append("")
            continue
        if stripped:
            collected.append(stripped)
    text = "\n".join(collected).strip()
    return text or None


def _is_plan_task(task) -> bool:
    flow_id = (getattr(task, "flow_template_id", "") or "").lower()
    if "plan" in flow_id:
        return True
    labels = getattr(task, "labels", []) or []
    return any("plan" in str(lbl).lower() for lbl in labels)


def _is_in_review(task) -> bool:
    status = getattr(task, "work_status", None)
    if status is None:
        return False
    value = getattr(status, "value", str(status))
    return value == "review"


# ---------------------------------------------------------------------------
# Task → API conversion
# ---------------------------------------------------------------------------


def _task_to_summary(task) -> APITaskSummary:
    return APITaskSummary(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        work_status=_enum_value(task.work_status),
        type=_enum_value(task.type),
        priority=_enum_value(task.priority),
        assignee=task.assignee,
        current_node_id=task.current_node_id,
        plan_version=getattr(task, "plan_version", None),
        updated_at=getattr(task, "updated_at", None),
    )


def _task_to_detail(task, *, plan: APIPlan | None = None) -> APITaskDetail:
    relationships = APITaskRelationships(
        parent=_pair_to_id(task.parent_project, task.parent_task_number),
        children=[_pair_to_id(p, n) for p, n in (task.children or [])],
        blocks=[_pair_to_id(p, n) for p, n in (task.blocks or [])],
        blocked_by=[_pair_to_id(p, n) for p, n in (task.blocked_by or [])],
        relates_to=[_pair_to_id(p, n) for p, n in (task.relates_to or [])],
        supersedes=_pair_to_id(task.supersedes_project, task.supersedes_task_number),
        superseded_by=_pair_to_id(
            task.superseded_by_project, task.superseded_by_task_number
        ),
    )
    transitions = [
        APITransition(
            from_state=t.from_state,
            to_state=t.to_state,
            actor=t.actor,
            timestamp=t.timestamp,
            reason=t.reason,
        )
        for t in (task.transitions or [])
    ]
    executions = [_execution_to_api(e) for e in (task.executions or [])]
    context = [
        APIContextEntry(
            actor=c.actor,
            timestamp=c.timestamp,
            text=c.text,
            entry_type=c.entry_type or "note",
        )
        for c in (task.context or [])
    ]
    return APITaskDetail(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        work_status=_enum_value(task.work_status),
        type=_enum_value(task.type),
        priority=_enum_value(task.priority),
        assignee=task.assignee,
        current_node_id=task.current_node_id,
        plan_version=getattr(task, "plan_version", None),
        updated_at=getattr(task, "updated_at", None),
        description=task.description or "",
        acceptance_criteria=task.acceptance_criteria,
        constraints=task.constraints,
        labels=task.labels or [],
        relevant_files=task.relevant_files or [],
        relationships=relationships,
        flow_template_id=task.flow_template_id or None,
        flow_template_version=task.flow_template_version,
        requires_human_review=getattr(task, "requires_human_review", False),
        predecessor_task_id=getattr(task, "predecessor_task_id", None),
        transitions=transitions,
        executions=executions,
        context=context,
        external_refs=task.external_refs or {},
        total_input_tokens=getattr(task, "total_input_tokens", 0),
        total_output_tokens=getattr(task, "total_output_tokens", 0),
        session_count=getattr(task, "session_count", 0),
        created_at=task.created_at,
        created_by=task.created_by or "",
        plan=plan,
    )


def _execution_to_api(execution) -> APIFlowNodeExecution:
    work_output: APIWorkOutput | None = None
    if execution.work_output is not None:
        artifacts = [
            APIArtifact(
                kind=_enum_value(a.kind),
                description=a.description,
                ref=a.ref,
                path=a.path,
                external_ref=a.external_ref,
            )
            for a in (execution.work_output.artifacts or [])
        ]
        work_output = APIWorkOutput(
            type=_enum_value(execution.work_output.type),
            summary=execution.work_output.summary,
            artifacts=artifacts or None,
        )
    return APIFlowNodeExecution(
        task_id=execution.task_id,
        node_id=execution.node_id,
        visit=execution.visit,
        status=_enum_value(execution.status),
        decision=_enum_value(execution.decision) if execution.decision else None,
        decision_reason=execution.decision_reason,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        work_output=work_output,
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _pair_to_id(project: str | None, number: int | None) -> str | None:
    if not project or number is None:
        return None
    return f"{project}/{number}"


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------


def list_inbox(
    config: PollyPMConfig,
    *,
    project: str | None = None,
    type_filter: str | None = None,
    state_filter: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[APIInboxItem], str | None]:
    """Aggregate inbox view across one or all projects.

    Mirrors :func:`pollypm.cockpit_inbox.render_inbox_panel` at a
    coarser granularity — the API only exposes the typed shape per
    spec §7. Projects with no inbox state contribute nothing.
    """
    items = _collect_inbox_items(config, project=project)
    if type_filter is not None:
        items = [i for i in items if i.type == type_filter]
    if state_filter is not None:
        items = [i for i in items if i.state == state_filter]
    items.sort(key=lambda item: item.updated_at, reverse=True)

    cursor_idx = 0
    if cursor is not None:
        for idx, item in enumerate(items):
            if item.id == cursor:
                cursor_idx = idx + 1
                break
    page = items[cursor_idx : cursor_idx + limit]
    next_cursor: str | None = None
    if cursor_idx + limit < len(items) and page:
        next_cursor = page[-1].id
    return page, next_cursor


def get_inbox_item(config: PollyPMConfig, item_id: str) -> APIInboxItemDetail | None:
    items = _collect_inbox_items(config, project=None)
    target = next((item for item in items if item.id == item_id), None)
    if target is None:
        return None
    messages = _load_inbox_messages(config, target)
    return APIInboxItemDetail(
        id=target.id,
        project=target.project,
        type=target.type,
        state=target.state,
        subject=target.subject,
        preview=target.preview,
        owner=target.owner,
        thread_id=target.thread_id,
        created_at=target.created_at,
        updated_at=target.updated_at,
        metadata=target.metadata,
        messages=messages,
    )


def _collect_inbox_items(
    config: PollyPMConfig, *, project: str | None
) -> list[APIInboxItem]:
    """Load inbox items from the work-service for the requested projects.

    "Inbox" here = chat-flow tasks + plan-review tasks. Mirrors the
    set the cockpit panel surfaces. Each task becomes one inbox
    entry; ``id`` is the task_id so later detail / reply paths can
    address it.
    """
    out: list[APIInboxItem] = []
    keys: Iterable[str]
    if project is not None:
        keys = (project,) if project in config.projects else ()
    else:
        keys = config.projects.keys()

    now = datetime.now(timezone.utc)
    for key in keys:
        proj = config.projects[key]
        try:
            with _open_work_service_readonly(
                config=config, project_key=key, project_path=proj.path
            ) as svc:
                tasks = svc.list_tasks(project=key)
                # Snooze visibility (#2060): the snooze write helper
                # persists ``entry_type='snooze'`` rows whose text
                # encodes the wake-up time. Items whose latest snooze
                # is still in the future must NOT appear in the
                # default inbox view (otherwise the endpoint returns
                # 200 while the row stays actionable). We compute the
                # active-snooze set once per project — the same
                # readonly service handle stays open so we don't
                # double-pay for connection setup.
                snoozed_ids = _active_snoozed_ids(svc, tasks, now=now)
                # Iterate inside the ``with`` block so the canonical
                # inbox predicate (Codex round-5 on #2060) can call
                # ``svc.get_flow(...)`` for its current-node-human
                # branch while the readonly handle is still open.
                #
                # One shared ``flow_cache`` per project scan: the
                # canonical predicate falls back to ``svc.get_flow``
                # for the current-node-human branch, and a page of N
                # tasks on the same flow would otherwise pay N
                # lookups. Matches the cockpit / rail / dashboard
                # path in :func:`pollypm.work.inbox_view.inbox_tasks`
                # (one cache, threaded through every call). Codex
                # round-6 blocker 2 on PR #2060.
                flow_cache: dict = {}
                for task in tasks:
                    if task.task_id in snoozed_ids:
                        continue
                    entry = _task_to_inbox_item(task, svc, flow_cache=flow_cache)
                    if entry is not None:
                        out.append(entry)
        except _BACKING_STORE_ERRORS as exc:
            # Backing-store failure on a single project: log loudly,
            # skip that project but keep building the aggregate. We
            # intentionally don't 503 the whole inbox — the dashboard
            # would rather show 4-of-5 projects than fail open. Use
            # ``warning`` so this is visible without DEBUG and add
            # exc_info so the stack lands in the operator's logs.
            logger.warning(
                "inbox: backing store error for %s; skipping: %s",
                key,
                exc,
                exc_info=True,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Snooze visibility helpers (#2060)
#
# The POST /inbox/{id}/snooze endpoint persists an ``entry_type='snooze'``
# row whose text starts with ``until_iso=<ISO>; snoozed until <ISO>``.
# The wake-time parser + "still snoozed?" predicate live in
# ``pollypm.work.inbox_snooze`` so cockpit can adopt them WITHOUT
# duplicating regex logic (Codex round-2 ask on PR #2060). The
# module-private aliases here keep the existing import sites + tests
# working unchanged.
#
# ``_active_snoozed_ids`` calls ``svc.latest_snoozes_bulk(...)``
# (single SQL on pg) instead of the original per-task
# ``svc.get_context(entry_type='snooze', limit=1)`` loop — the
# inbox-list path is user-facing and a 50-task page was paying N
# round-trips to the work-service per request.
# ---------------------------------------------------------------------------

from pollypm.work.inbox_snooze import (
    is_snooze_active as _is_snooze_active,
    parse_snooze_until as _parse_snooze_until,
)


def _task_key(task_id: str) -> tuple[str, int]:
    """Split ``project/n`` into a ``(project, number)`` tuple.

    The bulk snooze helper keys by ``(project, task_number)`` (mirrors
    the underlying ``work_context_entries`` PK shape); this just
    centralises the parse so the call site doesn't sprout an ad-hoc
    splitter.
    """
    project, num = task_id.split("/", 1)
    return project, int(num)


def _active_snoozed_ids(
    svc, tasks, *, now: datetime,
) -> set[str]:
    """Return task_ids whose latest snooze entry is still in the future.

    Uses :meth:`WorkService.latest_snoozes_bulk` — one SQL query
    regardless of task count — instead of the per-task
    ``get_context(entry_type='snooze', limit=1)`` loop the round-1
    implementation shipped. Inbox listing is a user-facing scan path;
    a 50-row page was paying 50 round-trips before this lands
    (#2060 round-2). Items without a snooze row, or whose latest
    snooze has expired, are NOT in the returned set (the inbox shows
    them as actionable, matching cockpit semantics).

    Falls back to the per-task loop when the backing service lacks
    the bulk method (older mocks, alternate backends) so this stays
    safe to land before every implementation grows the helper.
    """
    if not tasks:
        return set()
    bulk = getattr(svc, "latest_snoozes_bulk", None)
    if bulk is not None:
        try:
            keys = [_task_key(t.task_id) for t in tasks]
            latest = bulk(keys)
        except Exception:  # noqa: BLE001 — readonly view degrades open
            logger.debug(
                "inbox: bulk snooze lookup failed; falling back to per-task",
                exc_info=True,
            )
            latest = None
        if latest is not None:
            snoozed: set[str] = set()
            for task in tasks:
                key = _task_key(task.task_id)
                entry = latest.get(key)
                if entry is None:
                    continue
                if _is_snooze_active(entry.text, now=now):
                    snoozed.add(task.task_id)
            return snoozed
    # Fallback: per-task loop (legacy path, kept for backends without
    # the bulk helper). This branch should not run against pg.
    snoozed = set()
    for task in tasks:
        try:
            entries = svc.get_context(
                task.task_id, entry_type="snooze", limit=1,
            )
        except Exception:  # noqa: BLE001 — readonly view degrades open
            logger.debug(
                "inbox: snooze lookup failed for %s",
                task.task_id, exc_info=True,
            )
            continue
        if not entries:
            continue
        if _is_snooze_active(entries[0].text, now=now):
            snoozed.add(task.task_id)
    return snoozed


def _is_inbox_member(task, svc=None, flow_cache=None) -> bool:
    """Return True iff ``task`` belongs to the API inbox surface.

    Thin delegate to :func:`pollypm.work.inbox_view.is_inbox_task` —
    the canonical predicate used by the cockpit inbox panel, the
    dashboard inbox count, and the rail badge. Routing the API
    write resolution through the same predicate closes Codex round-5
    blocker on #2060: the previous web-layer predicate accepted any
    ``flow_template_id == 'chat'`` plus substring plan labels (so
    ``not_plan_review`` / ``planning`` matched), widening the write
    surface beyond what GET /inbox / cockpit ever surfaces.

    ``svc`` is optional only so legacy callers without a flow-lookup
    handle (e.g. unit tests that construct stubs by hand) can still
    reach the helper; production callers always pass the live work
    service so the canonical predicate can resolve the current node
    when it falls back to the human-actor check. When ``svc`` is
    omitted we hand the predicate a no-flow shim so it degrades to
    the role / label branches only.

    ``flow_cache`` is an optional ``{(name, version): FlowTemplate}``
    dict — the same shape :mod:`pollypm.work.inbox_view` uses to make
    sure scanning N tasks on a shared flow runs ``svc.get_flow(...)``
    once, not N times. ``_collect_inbox_items`` builds one cache per
    request and threads it through; single-shot callers (write helpers
    resolving one task) can leave it ``None`` and pay one lookup —
    that's still an improvement over the per-call fresh cache the
    previous shape created. Codex round-6 blocker 2 on PR #2060.
    """
    flow_lookup = svc if svc is not None else _NoFlowLookup()
    return bool(is_inbox_task(task, flow_lookup, flow_cache=flow_cache))


class _NoFlowLookup:
    """Flow-lookup shim used when no work service handle is on hand.

    ``is_inbox_task`` falls back to ``service.get_flow(...)`` for its
    "current node is human" branch. When the caller has no service
    (rare; legacy tests) we return ``None`` so the canonical
    predicate's existing ``try/except`` short-circuits that branch
    cleanly. Roles and exact ``plan_review`` label checks still run.
    """

    def get_flow(self, name, project=None):  # noqa: D401 - protocol shim
        return None


def _task_to_inbox_item(task, svc=None, flow_cache=None) -> APIInboxItem | None:
    if not _is_inbox_member(task, svc, flow_cache=flow_cache):
        return None
    flow = (getattr(task, "flow_template_id", "") or "").lower()
    labels = [str(lbl) for lbl in (getattr(task, "labels", []) or [])]
    # Exact-equality on ``plan_review`` (NOT substring) so labels like
    # ``not_plan_review`` / ``planning`` cannot get classified as
    # ``type=plan_review`` items. Mirrors the canonical
    # :func:`pollypm.work.inbox_view._is_plan_review_label` predicate
    # the write gate uses — Codex round-6 blocker 3 on PR #2060.
    is_plan_review = any(lbl == "plan_review" for lbl in labels)
    is_chat = flow == "chat"
    item_type = "plan_review" if is_plan_review and not is_chat else "message"
    state = _inbox_state_from_task(task)
    if state == "closed":
        return None
    body = task.description or ""
    preview = body.strip().splitlines()[0] if body.strip() else None
    metadata: dict[str, Any] = {
        "task_id": task.task_id,
        "labels": labels,
        "flow_template_id": task.flow_template_id,
    }
    if is_plan_review:
        metadata["judgment_calls"] = _extract_judgment_calls(body)
    return APIInboxItem(
        id=task.task_id,
        project=task.project,
        type=item_type,
        state=state,
        subject=task.title,
        preview=preview,
        owner=_inbox_owner_for_task(task),
        thread_id=task.task_id,
        created_at=task.created_at or datetime.utcnow(),
        updated_at=task.updated_at or task.created_at or datetime.utcnow(),
        metadata=metadata,
    )


def _inbox_state_from_task(task) -> str:
    status = getattr(task, "work_status", None)
    value = getattr(status, "value", str(status)) if status else ""
    if value in {"done", "cancelled"}:
        return "closed"
    if value == "review":
        return "waiting-on-pm"
    if value in {"in_progress", "queued", "draft"}:
        return "open"
    if value in {"blocked", "on_hold", "rework"}:
        return "open"
    return "open"


def _inbox_owner_for_task(task) -> str:
    roles = getattr(task, "roles", {}) or {}
    operator = str(roles.get("operator", "")).lower()
    if operator in {"pm", "pa", "worker", "operator"}:
        return operator
    if "architect" in operator:
        return "pm"
    return "pm"


def _load_inbox_messages(config: PollyPMConfig, item: APIInboxItem) -> list[APIInboxMessage]:
    """Pull the context-log entries that drive an inbox thread."""
    project = config.projects.get(item.project)
    if project is None:
        return []

    out: list[APIInboxMessage] = []
    try:
        with _open_work_service_readonly(
            config=config, project_key=item.project, project_path=project.path
        ) as svc:
            entries = svc.get_context(item.id) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbox: get_context failed for %s: %s", item.id, exc)
        return out
    for idx, entry in enumerate(entries):
        out.append(APIInboxMessage(
            id=f"{item.id}#{idx}",
            sender=entry.actor or "operator",
            timestamp=entry.timestamp,
            body=entry.text,
        ))
    return out


# ---------------------------------------------------------------------------
# Audit-log → API event
# ---------------------------------------------------------------------------


def audit_event_to_api(event: AuditEvent) -> APIEvent:
    ts = _parse_iso(event.ts) or datetime.utcnow()
    return APIEvent.model_validate({
        "schema": event.schema,
        "ts": ts,
        "project": event.project,
        "event": event.event,
        "subject": event.subject,
        "actor": event.actor,
        "status": event.status,
        "metadata": event.metadata or {},
    })


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_api_config(config_path: Path | None) -> PollyPMConfig:
    """Load PollyPM config without bringing tmux / supervisor along.

    The API server intentionally avoids ``PollyPMService.load_supervisor``
    so it stays usable with the cockpit down. ``load_config`` is the
    shared, side-effect-free path the cockpit dashboard also uses.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH

    return load_config(config_path or DEFAULT_CONFIG_PATH)


__all__ = [
    "archive_inbox_item",
    "archive_project",
    "audit_event_to_api",
    "get_active_plan",
    "get_inbox_item",
    "get_project",
    "get_task_detail",
    "init_project_guide_for_role",
    "list_inbox",
    "list_project_tasks",
    "list_projects",
    "load_api_config",
    "mark_read_inbox_item",
    "project_drilldown",
    "promote_inbox_to_task",
    "queue_task",
    "reply_inbox_item",
    "set_project_tracked",
    "snooze_inbox_item",
]
