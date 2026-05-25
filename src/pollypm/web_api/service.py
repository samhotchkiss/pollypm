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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite3

import psycopg
import psycopg_pool

from pollypm.audit.log import AuditEvent, read_events
from pollypm.config import PollyPMConfig, load_config
from pollypm.models import KnownProject
from pollypm.work import inbox_snooze as _inbox_snooze
from pollypm.work.inbox_view import (
    inbox_item_type_for_task,
    inbox_state_for_task,
    inbox_task_matches_type,
    is_archived_inbox_task,
    is_inbox_task,
    is_inbox_task_identity,
)
from pollypm.work.models import TaskSummaryCursorError, TaskSummaryProjection
from pollypm.web_api.errors import (
    APIError,
    not_found,
    service_unavailable,
    too_many_requests,
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
from pollypm.audit.log import (
    EVENT_TASK_CANCEL_CONFIRMED,
    EVENT_TASK_CANCEL_WARNED,
)
from pollypm.work.cancel_safety import (
    emit_cancel_safety_event,
    in_progress_assignee,
)

logger = logging.getLogger(__name__)

_is_snooze_active = _inbox_snooze.is_snooze_active
_parse_snooze_until = _inbox_snooze.parse_snooze_until


@dataclass(frozen=True)
class InboxListPage:
    items: list[APIInboxItem]
    next_cursor: str | None
    total: int
    has_more: bool
    unread_count: int


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


def list_active_worker_sessions_strict(
    config: PollyPMConfig, *, project: str | None = None
) -> list[Any]:
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
    project_filter = project
    project_settings = getattr(config, "project", None)
    if project_settings is None:
        return []
    project_key = getattr(project_settings, "name", "")
    project_path = getattr(project_settings, "root_dir", None)
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
                if project_filter is None:
                    records = list_fn(active_only=True)
                else:
                    records = list_fn(
                        project=project_filter, active_only=True
                    )
            except TypeError:
                records = list_fn(active_only=True)
            return list(records or [])
    except Exception as exc:  # noqa: BLE001
        raise WorkServiceFacadeUnavailable(str(exc)) from exc


def list_active_worker_sessions(
    config: PollyPMConfig, *, project: str | None = None
) -> list[Any]:
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
    project_filter = project
    project_settings = getattr(config, "project", None)
    if project_settings is None:
        return []
    project_key = getattr(project_settings, "name", "")
    project_path = getattr(project_settings, "root_dir", None)
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
                try:
                    if project_filter is None:
                        records = list_fn(active_only=True)
                    else:
                        records = list_fn(
                            project=project_filter, active_only=True
                        )
                except TypeError:
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
    response matches what the cockpit dashboard renders. On the pg
    backend, collapse the historical per-project fanout into one bulk
    task read so dashboard/project-list polls do not open and query the
    work service once per registered project.
    """
    out: list[APIProject] = []
    snapshots = _project_task_snapshots(config)
    for key, project in config.projects.items():
        if tracked_only and not project.tracked:
            continue
        if snapshots is not None:
            out.append(
                _project_to_api_from_tasks(
                    config, key, project, snapshots.get(key, [])
                )
            )
        else:
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
    project = config.projects.get(key)
    if project is None:
        return None
    project_path = project.path

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
    counts: dict[str, int] = {}
    pending_plan_review = False
    open_inbox_count = 0
    try:
        with _open_work_service_readonly(
            config=config, project_key=key, project_path=project_path
        ) as svc:
            try:
                counts = svc.state_counts(project=key) or {}
            except Exception:  # noqa: BLE001
                counts = {}
            try:
                review_tasks = svc.list_tasks(project=key, work_status="review")
            except Exception:  # noqa: BLE001
                review_tasks = []
            pending_plan_review = any(_is_plan_task(t) for t in review_tasks)
            open_inbox_count = _count_open_inbox_with_service(svc, key)
            tasks = svc.list_tasks(project=key, limit=10)
            for task in tasks:
                top_tasks.append(_task_to_summary(task))
            plan = _active_plan_from_review_tasks(svc, review_tasks)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drilldown: work-service open failed for %s: %s", key, exc)

    base = _project_to_api_from_metrics(
        config,
        key,
        project,
        counts=counts,
        pending_plan_review=pending_plan_review,
        open_inbox_count=open_inbox_count,
    )
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


# Codex round 7 on #2063 moved the config-write lock primitive into
# :mod:`pollypm.config` (``config_rmw_lock``) so CLI / cockpit / onboarding
# writers participate in the same serialisation invariant. Round 6's
# local ``_config_write_lock`` only serialised API-vs-API; a CLI write
# that landed between an API helper's ``load_config`` and
# ``write_config`` was still silently overwritten by the API's older
# snapshot (the exact reproduction Codex documented on round 7).
#
# The API helpers below import ``config_rmw_lock`` from ``pollypm.config``
# and wrap their full read-modify-write under it. Every in-tree config
# writer that does a ``load_config → mutate → write_config`` sequence
# wraps it under the shared lock — see ``pollypm.projects`` (including
# ``scan_projects`` post round 9), ``pollypm.accounts``,
# ``pollypm.workers``, ``pollypm.onboarding``, ``pollypm.cockpit_ui``,
# ``pollypm.cockpit_project_settings``, and
# ``pollypm.plugins_builtin.project_planning.cli.project``. The
# ``write_example_config`` initialiser in ``pollypm.config`` also wraps
# its write so two concurrent ``pm init`` callers serialise on the same
# lock. Two writes are intentionally OUTSIDE a full RMW wrap and rely
# on :func:`pollypm.config.write_config`'s internal lock (Pattern A)
# for torn-write protection:
#
# 1. ``pollypm.onboarding_tui`` first-run build+write — no prior
#    snapshot to merge against; the API/CLI is not running yet.
# 2. ``pollypm.config.load_config`` auth-token mint — re-reads under
#    the lock before the write so concurrent edits still survive
#    (round 9 audit fix).


def _refresh_live_projects(
    live_config: PollyPMConfig, fresh_config: PollyPMConfig
) -> None:
    """Replace ``live_config.projects`` in place with ``fresh_config.projects``.

    Used after every config-mutation path so the in-request
    :class:`ConfigDep` snapshot reflects disk state in full — both the
    mutated project AND any concurrent external edits / additions /
    removals — before we build the response body off it.

    Post-#2056 the ``ConfigDep`` provider reloads via
    ``load_config(config_path)`` per request, so cross-request
    consistency is already guaranteed by the reload. This refresh is
    therefore load-bearing only for:

    * the IN-REQUEST response (we serialize from
      ``config.projects[key]`` after the refresh, so it picks up the
      post-write disk state without an extra ``_project_to_api`` arg
      rewrite), AND
    * the in-memory fallback path (``config_path is None``), where
      tests construct :class:`PollyPMConfig` directly and the app
      factory wires a startup-frozen ``lambda: config`` provider with
      no per-request reload.

    Codex round 3 on #2063: a previous field-by-field copy
    (``tracked`` / ``path`` / ``name``) left other ``KnownProject``
    fields (``persona_name``, ``kind``, role assignments, worker caps,
    plan enforcement, etc.) stale when an external edit changed them on
    the same project before the API call landed. Full ``clear() +
    update()`` propagates every field plus external add/remove on other
    keys.

    Mutates ``live_config.projects`` in place so any external object
    holding a reference to the dict sees the new contents.
    """
    live_config.projects.clear()
    live_config.projects.update(fresh_config.projects)


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

    * The entire read-modify-write section is serialised by the
      SHARED :func:`pollypm.config.config_rmw_lock` (Codex round 7 on
      #2063 promoted the round-6 API-local flock to a project-wide
      invariant). Every other in-tree config writer — ``pm
      add-project`` / ``pm rename-project`` / ``pm remove-project``,
      the cockpit role-assignment editor, accounts, workers, the
      project-planning plugin's session-purge — wraps its full
      load → mutate → write under the same lock, so a CLI write that
      lands while we're inside this block blocks on the flock instead
      of clobbering our snapshot (or being clobbered by us). The
      lockfile is a sibling of ``config_path`` (``<path>.lock``); the
      lock is per-path so two unrelated configs (multi-tenant dev
      setups) don't serialise against each other.
    * Re-load the on-disk TOML so any external CLI / cockpit edits
      that landed between server boot and this call are preserved —
      we never write back the long-lived ``ConfigDep`` snapshot with
      ``force=True`` (that would silently drop e.g. a project added
      via ``pm add-project`` after the server started). Post-#2056
      the request's ``config`` is itself a per-request
      ``load_config(config_path)`` reload, so the extra
      ``load_config`` here is a cheap cache hit on the production
      path AND still required for the in-memory fallback path
      (``config_path is None``) where ``config`` is a startup-frozen
      object.
    * Idempotency is decided AGAINST the freshly-loaded disk state
      (Codex round 2 on #2063): if disk already matches ``tracked``
      we skip the write AND refresh the request's snapshot from disk
      so the response body is built from disk state, not from any
      stale field.
    * Mutate the freshly-loaded copy via DEEP COPY, NOT the live
      ``config`` — ``load_config`` is memoised by path, so the
      ``fresh_cached`` snapshot IS the same object as the request's
      ``config`` whenever the server was bootstrapped via
      ``load_config(config_path)``. Without the deep copy, a write
      failure after mutating ``fresh.tracked`` would leak a stale
      lie into the cache (Codex round 4 on #2063).
    * After every successful disk write we re-load disk and FULLY
      refresh ``config.projects`` via :func:`_refresh_live_projects`
      so the in-request response reflects the post-write state plus
      any concurrent external edits (Codex round 3 on #2063). Post-
      #2056 the NEXT request reloads from disk anyway, so this is
      mainly load-bearing for the current response and for the
      in-memory fallback path.
    * If ``write_config`` raises ``OSError`` the live ``config``
      stays untouched and a subsequent GET reflects the unchanged
      disk state — no stale in-memory lie that flips back on restart.

    Returns the post-mutation :class:`APIProject` snapshot so the
    client can refresh without a follow-up GET (same idiom as the
    task transitions in Phase 2).
    """
    import copy

    from pollypm.config import config_rmw_lock, load_config, write_config

    # Bind to the original (in-memory) project up-front so the 404 path
    # doesn't pay for a disk reload or take the lock.
    live_project = config.projects.get(project_key)
    if live_project is None:
        raise not_found(f"Project not registered: {project_key}")

    config_path = _resolve_config_write_path(config)
    # Hold the SHARED per-config RMW lock across the entire
    # load → mutate → write → reload. Two concurrent API requests on
    # different projects can't lose each other's writes (Codex round
    # 6), AND a concurrent CLI/cockpit ``load_config → mutate →
    # write_config`` sequence on the same config can't slot in between
    # our load and our write to lose its edit (Codex round 7). The
    # lock is released BEFORE we return; subsequent callers'
    # load_config sees the updated mtime and reloads fresh.
    with config_rmw_lock(config_path):
        # Reload from disk so we don't lose concurrent CLI / cockpit
        # edits (e.g. an external ``pm add-project`` that added a new
        # project key the in-memory server hasn't seen yet). Under the
        # flock this also picks up any sibling-API-request mutation
        # that committed while we were waiting on the lock.
        try:
            fresh_cached = load_config(config_path)
        except OSError as exc:
            raise service_unavailable(
                f"Failed to reload config for {project_key}: {exc}",
                hint="Check read permissions on the PollyPM config file.",
            ) from exc
        # ``load_config`` is memoised by ``config_path`` (see
        # ``pollypm.config._config_cache``) so ``fresh_cached`` IS the
        # same object as the live ``ConfigDep`` whenever the server was
        # bootstrapped via ``load_config(config_path)`` — which is
        # exactly what ``pm serve`` / ``load_api_config`` do in
        # production. Mutating ``fresh_cached.projects[key].tracked``
        # would therefore mutate the live snapshot BEFORE the durable
        # write completes. If write_config then raises OSError, the API
        # returns 503 but the live config has already flipped — a
        # rollback violation Codex reproduced on round 4 of #2063. Take
        # a deep copy here so all mutation happens on a detached graph;
        # the live snapshot is only touched via
        # ``_refresh_live_projects`` AFTER the disk write succeeds.
        fresh = copy.deepcopy(fresh_cached)
        fresh_project = fresh.projects.get(project_key)
        if fresh_project is None:
            # Disk-side delete raced us. Treat as 404 — the in-memory
            # state is stale and the next GET will agree.
            raise not_found(f"Project not registered: {project_key}")

        # Idempotency decided against DISK, not against the long-lived
        # in-memory snapshot. If disk already matches the request we
        # still FULLY refresh the live ``config.projects`` from disk so
        # any external metadata edits (persona_name, kind, role
        # assignments, worker caps, etc.) propagate — Codex round 3 on
        # #2063.
        if fresh_project.tracked == tracked:
            _refresh_live_projects(config, fresh)
            return _project_to_api(
                config, project_key, config.projects[project_key]
            )

        fresh_project.tracked = tracked
        fresh.projects[project_key] = fresh_project
        try:
            write_config(fresh, config_path, force=True)
        except OSError as exc:
            # Live config untouched — Codex P0 #1 (rollback guarantee).
            # The deep-copy above is what makes this rollback real on
            # the cached-load_config path: ``fresh`` is a detached
            # graph, so mutating ``fresh_project.tracked`` never
            # reached the live ``ConfigDep`` shared with FastAPI
            # request handlers. Codex round 4 on #2063.
            raise service_unavailable(
                f"Failed to persist project state for {project_key}: {exc}",
                hint="Check write permissions on the PollyPM config file.",
            ) from exc

        # Disk write succeeded — re-load disk and FULLY refresh the
        # live ``config.projects`` so in-process callers see (a) the
        # new ``tracked`` we just wrote AND (b) any concurrent external
        # edits to other fields / other projects that landed between
        # our load and our write — Codex round 3 on #2063. The extra
        # load_config() is cheap relative to the durable write we just
        # did. The flock guarantees no other API writer is between our
        # write and this reload.
        try:
            post_write = load_config(config_path)
        except OSError as exc:
            # Live config still reflects pre-write state. Surface as
            # 503 so the client can retry; subsequent GETs stay
            # consistent.
            raise service_unavailable(
                f"Failed to reload config after writing {project_key}: {exc}",
                hint="Check read permissions on the PollyPM config file.",
            ) from exc
        _refresh_live_projects(config, post_write)

        refreshed_project = config.projects[project_key]

    # Audit emit outside the lock — it does its own I/O and we don't
    # want it serialising with other API writers.
    _emit_project_audit(
        event="projects.tracked.set",
        config=config,
        project_key=project_key,
        project_path=refreshed_project.path,
        actor=actor,
        reason=reason,
        extra={"tracked": tracked},
    )
    return _project_to_api(config, project_key, refreshed_project)


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

    Codex round 7 on #2063: the entire ``remove_project`` call + the
    post-write reload runs under the SHARED
    :func:`pollypm.config.config_rmw_lock`. The facade itself also
    acquires the lock (re-entrant per thread, so the nested acquire
    is a no-op). Without this shared invariant, an archive on project
    A and a CLI ``pm projects remove`` on project B could each load
    the same cached config, mutate their own key, and write — losing
    one mutation. The shared lock serialises the read-modify-write
    across BOTH API and CLI entry points (round 6's API-local flock
    only protected API-vs-API).
    """
    import typer

    from pollypm.config import config_rmw_lock
    from pollypm.projects import remove_project as _remove_project_facade

    live_project = config.projects.get(project_key)
    if live_project is None:
        raise not_found(f"Project not registered: {project_key}")

    label = live_project.display_label()
    project_path = live_project.path
    config_path = _resolve_config_write_path(config)
    # Hold the SHARED per-config RMW lock across the facade call AND
    # the post-archive reload so we serialise with any concurrent
    # API ``set_project_tracked`` / sibling ``archive_project`` call
    # AND with concurrent CLI / cockpit writers (Codex round 7 on
    # #2063). The lock is released BEFORE the audit emit; subsequent
    # writers' load_config sees our committed mtime and reloads fresh.
    with config_rmw_lock(config_path):
        # Snapshot the live project entry so we can roll back the in-
        # memory state if ``remove_project`` raises OSError after
        # mutation. The facade does ``config = load_config(...);
        # del config.projects[key]; write_config(...)`` — and
        # ``load_config`` is memoised by path, so the dict it mutates
        # IS the live ``ConfigDep`` dict on the production cached path.
        # A write failure after ``del`` would otherwise leave the live
        # snapshot missing the project even though disk still has it
        # (Codex round 4 on #2063, mirror of the set_project_tracked
        # deep-copy fix above).
        _live_project_snapshot = config.projects.get(project_key)
        try:
            # ``remove_project`` reloads from disk, enforces the
            # session-ref guard, then writes back. Live ``config`` is
            # untouched if the facade raises ``BadParameter`` (it
            # raises before mutating); OSError can fire after the in-
            # place delete, so we restore the snapshot below.
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
            # Disk-side race (project gone between our 404 check and
            # the facade's reload). Treat as 404 so the client sees a
            # stable error code.
            raise not_found(msg) from exc
        except OSError as exc:
            # Roll the live snapshot back if the facade already del'd
            # the cached entry before the write failed — Codex round 4
            # on #2063 (durable-write rollback on the cached config
            # path).
            if (
                _live_project_snapshot is not None
                and project_key not in config.projects
            ):
                config.projects[project_key] = _live_project_snapshot
            raise service_unavailable(
                f"Failed to persist archive for {project_key}: {exc}",
                hint="Check write permissions on the PollyPM config file.",
            ) from exc

        # Disk write succeeded — re-load disk and FULLY refresh the
        # live ``config.projects`` (Codex round 3 on #2063). The
        # previous pop()-only sync left any concurrent external
        # project ADDITIONS invisible to subsequent in-process
        # ``GET /projects`` calls until the server restarted. Full
        # refresh both drops the archived key and surfaces any
        # external additions / metadata edits.
        try:
            post_archive = load_config(config_path)
        except OSError as exc:
            # Archive landed on disk but we can't see the post-state.
            # Best-effort: drop the archived key from the live snapshot
            # so the immediate response is at least internally
            # consistent.
            config.projects.pop(project_key, None)
            raise service_unavailable(
                f"Failed to reload config after archiving {project_key}: {exc}",
                hint="Check read permissions on the PollyPM config file.",
            ) from exc
        _refresh_live_projects(config, post_archive)

    # Audit emit outside the lock — independent I/O.
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


_TERMINAL_STATUS_VALUES = frozenset({"done", "cancelled"})


def _status_value(task: object) -> str:
    status = getattr(task, "work_status", None)
    return str(getattr(status, "value", status) or "")


def _zero_counts() -> dict[str, int]:
    try:
        from pollypm.work.models import WorkStatus

        return {status.value: 0 for status in WorkStatus}
    except Exception:  # noqa: BLE001
        return {}


def _counts_from_tasks(tasks: list[object]) -> dict[str, int]:
    counts = _zero_counts()
    for task in tasks:
        status = _status_value(task)
        if not status:
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


def _open_inbox_count_from_tasks(tasks: list[object]) -> int:
    count = 0
    for task in tasks:
        if getattr(task, "flow_template_id", "") != "chat":
            continue
        if _status_value(task) not in _TERMINAL_STATUS_VALUES:
            count += 1
    return count


def _project_task_snapshots(
    config: PollyPMConfig,
) -> dict[str, list[object]] | None:
    """Return pg task rows grouped by registered project, or ``None``.

    This is an optimization only. If the pg aggregate path is unavailable
    the callers fall back to the existing per-project service reads.
    """
    try:
        from pollypm.storage._backend_dispatch import is_pg_backend

        if not is_pg_backend(config):
            return None
        from pollypm.cockpit_pg_aggregates import (
            _all_tasks_grouped_uncached,
            all_tasks_for_project,
        )

        # API responses should reflect the latest committed task state.
        # Use the same one-query pg aggregate as the cockpit, but bypass
        # the cockpit's short TTL cache so concurrent agents do not see
        # deliberately stale project badges/counts through REST.
        grouped = _all_tasks_grouped_uncached(config)
        if grouped is None:
            return None
        return {
            key: all_tasks_for_project(grouped, config, key)
            for key in config.projects
        }
    except Exception:  # noqa: BLE001
        logger.debug("project task snapshot aggregate unavailable", exc_info=True)
        return None


def _project_to_api_from_tasks(
    config: PollyPMConfig,
    key: str,
    project: KnownProject,
    tasks: list[object],
) -> APIProject:
    counts = _counts_from_tasks(tasks)
    pending_plan_review = any(
        _status_value(task) == "review" and _is_plan_task(task)
        for task in tasks
    )
    open_inbox_count = _open_inbox_count_from_tasks(tasks)
    last_activity_at = _max_datetime(
        _latest_task_activity(tasks),
        _latest_audit_activity(key, project),
    )
    return _project_to_api_from_metrics(
        config,
        key,
        project,
        counts=counts,
        pending_plan_review=pending_plan_review,
        open_inbox_count=open_inbox_count,
        last_activity_at=last_activity_at,
    )


def _project_to_api_from_metrics(
    config: PollyPMConfig,  # noqa: ARG001 — kept for callsite symmetry
    key: str,
    project: KnownProject,
    *,
    counts: dict[str, int],
    pending_plan_review: bool,
    open_inbox_count: int,
    last_activity_at: datetime | None = None,
) -> APIProject:
    glyph = _glyph_for_project(
        project, counts, pending_plan_review, open_inbox_count
    )
    if not project.tracked:
        glyph = "paused"
    return APIProject(
        key=key,
        name=project.display_label(),
        path=str(project.path),
        tracked=project.tracked,
        kind=project.kind.value if hasattr(project.kind, "value") else str(project.kind),
        persona_name=project.persona_name,
        state=None,
        glyph=glyph,
        task_counts=counts,
        open_inbox_count=open_inbox_count,
        pending_plan_review=pending_plan_review,
        last_activity_at=last_activity_at,
    )


def _project_to_api(config: PollyPMConfig, key: str, project: KnownProject) -> APIProject:
    counts: dict[str, int] = {}
    pending_plan_review = False
    open_inbox_count = 0
    last_activity_at: datetime | None = None

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
            try:
                latest_tasks = svc.list_tasks(project=key, limit=200)
                last_activity_at = _latest_task_activity(latest_tasks)
            except Exception:  # noqa: BLE001
                last_activity_at = None
    except Exception as exc:  # noqa: BLE001
        logger.debug("project counts: work-service unavailable for %s: %s", key, exc)

    try:
        open_inbox_count = _count_open_inbox(config, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project inbox count failed for %s: %s", key, exc)

    last_activity_at = _max_datetime(
        last_activity_at,
        _latest_audit_activity(key, project),
    )
    return _project_to_api_from_metrics(
        config,
        key,
        project,
        counts=counts,
        pending_plan_review=pending_plan_review,
        open_inbox_count=open_inbox_count,
        last_activity_at=last_activity_at,
    )


def _max_datetime(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if _datetime_sort_value(a) >= _datetime_sort_value(b) else b


def _datetime_sort_value(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _latest_task_activity(tasks: Iterable[Any]) -> datetime | None:
    latest: datetime | None = None
    for task in tasks:
        updated = getattr(task, "updated_at", None)
        if isinstance(updated, datetime):
            latest = _max_datetime(latest, updated)
    return latest


def _latest_audit_activity(key: str, project: KnownProject) -> datetime | None:
    try:
        events = read_events(key, project_path=project.path, limit=1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project audit activity failed for %s: %s", key, exc)
        return None
    if not events:
        return None
    return _parse_iso(events[-1].ts)


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
    return _active_plan_task_for_project(svc, project_key) is not None


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
            return _count_open_inbox_with_service(svc, project_key)
    except Exception:  # noqa: BLE001
        return 0


def _count_open_inbox_with_service(svc: object, project_key: str) -> int:
    try:
        list_nonterminal = getattr(svc, "list_nonterminal_tasks", None)
        if callable(list_nonterminal):
            tasks = list_nonterminal(project=project_key)
        else:
            tasks = svc.list_tasks(project=project_key)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0
    return _open_inbox_count_from_tasks(list(tasks or []))


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
) -> tuple[list[APITaskSummary], str | None, int]:
    """Page of task summaries with cursor-based pagination.

    The cursor is the integer ``task_number`` of the last item in the
    previous page; absent ⇒ start. We sort tasks by ``task_number`` so
    the cursor is stable across calls without depending on
    ``updated_at``.
    """
    project = config.projects.get(project_key)
    if project is None:
        return [], None, 0

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
            total = len(tasks)
            if cursor_n is not None:
                tasks = [t for t in tasks if getattr(t, "task_number", 0) > cursor_n]
            page = tasks[:limit]
            next_cursor: str | None = None
            if len(tasks) > limit and page:
                next_cursor = str(getattr(page[-1], "task_number", 0))
            return [
                _task_to_summary(t) for t in page
            ], next_cursor, total
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


class StaleCursorError(Exception):
    """Raised when ``list_all_tasks`` is given a cursor that no longer
    matches any item in the current snapshot.

    Round 1 of Codex review on PR #2067 noted that the previous
    behaviour (silently restart at page one) creates duplicate rows
    and infinite-pagination loops under concurrent updates. We now
    raise this typed error so the route layer can map it to a 400
    ``invalid_request`` rather than mask the failure.
    """


def list_all_tasks(
    config: PollyPMConfig,
    *,
    project: str | None = None,
    statuses: list[str] | None = None,
    assignee: str | None = None,
    since: datetime | None = None,
    include_untracked: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[APITaskSummary], str | None, list[dict[str, object]], int]:
    """Cross-project flat task list (spec §5.1 / Phase 6 P0).

    The default scoped view iterates tracked projects, calls
    :meth:`list_tasks` per project, and concatenates. The
    ``include_untracked`` opt-in uses the work-service's workspace
    list query so rows outside ``config.projects`` can be surfaced
    without teaching the route layer about storage internals.

    * ``project`` — when set, restricts to a single project. The
      result is equivalent to ``list_project_tasks`` minus the 404 on
      unknown project (an unknown project here yields an empty page,
      matching the cross-project "no matches" semantics).
    * ``statuses`` — repeatable status filter; OR semantics. Empty /
      ``None`` means no status filter.
    * ``assignee`` — exact-match assignee filter.
    * ``since`` — only tasks with ``updated_at > since`` are returned.
      Must be timezone-aware; the route layer rejects naive ISO
      values before they reach this helper.
    * ``include_untracked`` — when false, restrict results to the
      tracked-project set and surface a warning when matching rows
      are omitted. When true, include rows for any project key stored
      in the work-service backing store.
    * Pagination uses an opaque cursor; the cursor encodes the
      ``(updated_at_iso, task_id)`` of the last item on the previous
      page. A cursor that no longer matches any item raises
      :class:`StaleCursorError` (route maps to 400) so clients restart
      from page one explicitly — silently restarting would create
      duplicate rows / infinite loops under concurrent updates
      (PR #2067 Codex round 1, P0 #3).

    Returns ``(page, next_cursor, warnings, total)``. ``warnings`` contains
    either per-project backing-store failures
    (``{"project": <key>, "error": <code>}``) or the tracked-scope
    filter warning (``{"code": "untracked_filtered", ...}``). An
    empty list means every project read succeeded and no matching
    rows were omitted.
    """
    status_filter = set(statuses or [])
    fast_page = _list_all_tasks_summary_page(
        config,
        project=project,
        statuses=statuses,
        assignee=assignee,
        since=since,
        include_untracked=include_untracked,
        limit=limit,
        cursor=cursor,
    )
    if fast_page is not None:
        return fast_page

    summaries: list[APITaskSummary] = []
    warnings: list[dict[str, object]] = []

    if include_untracked:
        tasks = _list_tasks_from_workspace(config, project=project)
        for task in tasks:
            if not _task_matches_list_filters(
                task,
                status_filter=status_filter,
                assignee=assignee,
                since=since,
            ):
                continue
            summaries.append(_task_to_summary(task))
        return _page_task_summaries(
            summaries, limit=limit, cursor=cursor, warnings=warnings
        )

    tracked_keys = _tracked_project_keys(config)
    keys: Iterable[str]
    if project is not None:
        keys = (project,) if project in tracked_keys else ()
    else:
        keys = tracked_keys

    for key in keys:
        proj = config.projects[key]
        try:
            with _open_work_service_readonly(
                config=config, project_key=key, project_path=proj.path
            ) as svc:
                # The work-service ``list_tasks`` only supports a
                # single ``work_status`` filter, so we apply the
                # OR-set client-side; assignee + since likewise apply
                # client-side to keep the wrapper portable across
                # backends.
                tasks = svc.list_tasks(project=key)
        except _BACKING_STORE_ERRORS as exc:
            logger.warning(
                "list_all_tasks: backing store error for %s; surfacing as warning: %s",
                key,
                exc,
                exc_info=True,
            )
            warnings.append({"project": key, "error": "service_unavailable"})
            continue

        for task in tasks:
            if not _task_matches_list_filters(
                task,
                status_filter=status_filter,
                assignee=assignee,
                since=since,
            ):
                continue
            summaries.append(_task_to_summary(task))

    try:
        dropped_count = _count_untracked_matches(
            config,
            tracked_keys=tracked_keys,
            project=project,
            status_filter=status_filter,
            assignee=assignee,
            since=since,
        )
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "list_all_tasks: could not inspect untracked task rows; "
            "surfacing workspace warning: %s",
            exc,
            exc_info=True,
        )
        warnings.append(
            {"project": "__workspace__", "error": "service_unavailable"}
        )
        dropped_count = 0
    if dropped_count:
        warnings.append(
            {
                "code": "untracked_filtered",
                "dropped_count": dropped_count,
                "reason": "untracked_projects",
            }
        )

    return _page_task_summaries(
        summaries, limit=limit, cursor=cursor, warnings=warnings
    )


def _list_all_tasks_summary_page(
    config: PollyPMConfig,
    *,
    project: str | None,
    statuses: list[str] | None,
    assignee: str | None,
    since: datetime | None,
    include_untracked: bool,
    limit: int,
    cursor: str | None,
) -> tuple[list[APITaskSummary], str | None, list[dict[str, object]], int] | None:
    workspace_root = _workspace_root_path(config)
    tracked_keys = _tracked_project_keys(config)
    # Multi-project reads keep the older per-project path so one failed
    # project still surfaces as a partial-failure warning.
    if not include_untracked and len(tracked_keys) > 1:
        return None
    try:
        with _open_work_service_readonly(
            config=config,
            project_key="__workspace__",
            project_path=workspace_root,
        ) as svc:
            list_page = getattr(svc, "list_task_summary_page", None)
            count_matches = getattr(svc, "count_task_summary_matches", None)
            if not callable(list_page) or not callable(count_matches):
                return None

            projects: tuple[str, ...] | None
            page_project: str | None = None
            if include_untracked:
                projects = None
                page_project = project
            elif project is not None:
                projects = (project,) if project in tracked_keys else ()
            else:
                projects = tracked_keys

            rows, next_cursor, total = list_page(
                projects=projects,
                project=page_project,
                work_statuses=tuple(statuses or ()),
                assignee=assignee,
                since=since,
                limit=limit,
                cursor=cursor,
            )
            warnings: list[dict[str, object]] = []
            if not include_untracked:
                if project is None:
                    dropped_count = count_matches(
                        exclude_projects=tracked_keys,
                        work_statuses=tuple(statuses or ()),
                        assignee=assignee,
                        since=since,
                    )
                elif project not in tracked_keys:
                    dropped_count = count_matches(
                        project=project,
                        work_statuses=tuple(statuses or ()),
                        assignee=assignee,
                        since=since,
                    )
                else:
                    dropped_count = 0
                if dropped_count:
                    warnings.append(
                        {
                            "code": "untracked_filtered",
                            "dropped_count": dropped_count,
                            "reason": "untracked_projects",
                        }
                    )
            return (
                [_task_summary_projection_to_api(row) for row in rows],
                next_cursor,
                warnings,
                total,
            )
    except TaskSummaryCursorError as exc:
        raise StaleCursorError(str(exc)) from exc
    except _BACKING_STORE_ERRORS:
        return None


def _tracked_project_keys(config: PollyPMConfig) -> tuple[str, ...]:
    return tuple(
        key
        for key, proj in config.projects.items()
        if getattr(proj, "tracked", True)
    )


def _workspace_root_path(config: PollyPMConfig) -> Path:
    return Path(
        getattr(config.project, "workspace_root", None)
        or getattr(config.project, "root_dir", None)
        or Path.cwd()
    )


def _task_for_summary(svc, task):
    try:
        return svc.get(task.task_id)
    except _BACKING_STORE_ERRORS:
        raise
    except Exception:  # noqa: BLE001
        return task


def _list_tasks_from_workspace(
    config: PollyPMConfig, *, project: str | None, hydrate: bool = False
):
    workspace_root = _workspace_root_path(config)
    with _open_work_service_readonly(
        config=config,
        project_key="__workspace__",
        project_path=workspace_root,
    ) as svc:
        tasks = svc.list_tasks(project=project)
        if hydrate:
            tasks = [_task_for_summary(svc, task) for task in tasks]
        return tasks


def _task_matches_list_filters(
    task,
    *,
    status_filter: set[str],
    assignee: str | None,
    since: datetime | None,
) -> bool:
    if status_filter:
        value = _enum_value(getattr(task, "work_status", ""))
        if value not in status_filter:
            return False
    if assignee is not None:
        if (getattr(task, "assignee", None) or "") != assignee:
            return False
    if since is not None:
        updated = getattr(task, "updated_at", None)
        if updated is not None and updated <= since:
            return False
    return True


def _count_untracked_matches(
    config: PollyPMConfig,
    *,
    tracked_keys: Iterable[str],
    project: str | None,
    status_filter: set[str],
    assignee: str | None,
    since: datetime | None,
) -> int:
    tracked = set(tracked_keys)
    if project is not None and project in tracked:
        return 0
    tasks = _list_tasks_from_workspace(config, project=project)
    dropped = 0
    for task in tasks:
        task_project = str(getattr(task, "project", ""))
        if project is None:
            if task_project in tracked:
                continue
        elif task_project != project:
            continue
        if _task_matches_list_filters(
            task,
            status_filter=status_filter,
            assignee=assignee,
            since=since,
        ):
            dropped += 1
    return dropped


def _page_task_summaries(
    summaries: list[APITaskSummary],
    *,
    limit: int,
    cursor: str | None,
    warnings: list[dict[str, object]],
) -> tuple[list[APITaskSummary], str | None, list[dict[str, object]], int]:
    # Newest-first ordering is the most useful default for cross-
    # project list views (cockpit "what changed recently?"). Tasks
    # without ``updated_at`` sort to the end via the epoch fallback so
    # the cursor encoding stays well-defined. Use a tz-aware epoch so
    # the sort key is comparable with the offset-aware ``updated_at``
    # values pg returns.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    summaries.sort(
        key=lambda t: (t.updated_at or epoch, t.task_id),
        reverse=True,
    )

    cursor_idx = 0
    if cursor is not None:
        matched = False
        for idx, item in enumerate(summaries):
            cursor_key = f"{(item.updated_at or epoch).isoformat()}|{item.task_id}"
            if cursor_key == cursor:
                cursor_idx = idx + 1
                matched = True
                break
        if not matched:
            # Empty-result + cursor is the only ambiguous case (the
            # caller could legitimately be paging past the end of a
            # now-empty list). Treat any other miss as a stale cursor
            # and force the client to restart explicitly.
            raise StaleCursorError(cursor)

    total = len(summaries)
    page = summaries[cursor_idx : cursor_idx + limit]
    next_cursor: str | None = None
    if cursor_idx + limit < len(summaries) and page:
        tail = page[-1]
        next_cursor = f"{(tail.updated_at or epoch).isoformat()}|{tail.task_id}"
    return page, next_cursor, warnings, total


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
            return _task_to_detail_with_plan(task, svc=svc)
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
                current = svc.get(task_id)
                if current.work_status.value != "draft":
                    raise APIError(
                        status_code=409,
                        code="invalid_state",
                        message=(
                            f"Cannot queue task in '{current.work_status.value}' state. "
                            "Task must be in 'draft' state."
                        ),
                        hint="Only draft tasks can be queued; refresh the task to see the current work_status.",
                    )
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
            # snapshot the client would see on a follow-up GET — that
            # includes plan hydration for plan-review tasks (#2064 r4).
            task = svc.get(task_id)
            return _task_to_detail_with_plan(task, svc=svc)
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


def claim_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    provision_scheduler: Callable[[Callable[[], None]], object] | None = None,
) -> tuple[APITaskDetail, list[str]]:
    """Atomically claim a queued task via the work-service.

    Mirrors ``pm work claim``. The work-service handles the
    queued-and-unblocked check, sets ``assignee``, advances to the
    flow's start node, and writes the transition row. We translate
    work-service exceptions into the API's typed error envelope.

    Routes through :func:`create_work_service_with_session` (the
    same facade ``pm task claim`` uses) so the SessionManager is
    wired BEFORE ``svc.claim`` fires — the API surface therefore
    provisions the per-task worker session, applies the parallel-
    cap check, and surfaces ``last_provision_error`` exactly like
    the CLI (#2064 round-9 blocker #2). The earlier head called
    ``create_work_service`` directly with no SessionManager wiring;
    a successful API claim would mark the task ``in_progress`` with
    no worker lane and no error feedback, silently breaking the
    operator workflow.

    Returns ``(task_detail, warnings)``. ``warnings`` is a (possibly
    empty) list of operator-facing strings that the route layer
    surfaces in ``TaskActionResult.warnings`` (#2064 round-10). The
    claim path uses it to forward ``svc.last_provision_error`` (post-
    commit worker-session failures captured by ``PgWorkService.claim``
    at ``pg_service.py:1905-1910``) AND
    ``svc._session_attach_error`` (SessionManager wire-up failures
    captured in ``service_factory.attach_session_manager``). Both
    surface with the same recovery wording the CLI emits at
    ``work/cli.py:912-929`` so cockpit operators see the same
    story regardless of channel.

    When ``provision_scheduler`` is supplied by the Web API route, the
    worker-session provisioning side effect is scheduled after the DB
    claim instead of blocking the HTTP response. The synchronous path is
    retained for CLI-like callers and tests that need immediate warning
    collection.
    """
    from pollypm.work.service_factory import (
        create_work_service_with_deferred_session,
        create_work_service_with_session,
    )
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
    )
    # #2064 round-11 blocker #1 — map cap back-pressure to a typed
    # 429 envelope instead of leaking ``WorkerCapExceededError`` as
    # 500 ``internal_error``. The exception lives in the session
    # manager module; lazy import keeps the web_api package free of
    # tmux/state-store imports at startup (same pattern the work-
    # service factory uses above).
    from pollypm.work.session_manager import WorkerCapExceededError

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    task_id = f"{project_key}/{task_number}"
    try:
        service_kwargs = {
            "config": config,
            "project_key": project_key,
            "project_path": project.path,
        }
        if provision_scheduler is None:
            work_service_cm = create_work_service_with_session(
                **service_kwargs
            )
        else:
            work_service_cm = create_work_service_with_deferred_session(
                **service_kwargs,
                schedule=provision_scheduler,
            )
        with work_service_cm as svc:
            try:
                svc.claim(task_id, actor)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc) or f"Task {task_id} cannot be claimed.",
                    hint="Only queued+unblocked tasks can be claimed.",
                ) from exc
            except WorkerCapExceededError as exc:
                # #2064 round-11 blocker #1: pre-claim cap probe at
                # ``PgWorkService.claim`` (``pg_service.py:1759-1763``)
                # raises ``WorkerCapExceededError`` from
                # ``SessionManager.check_parallel_cap`` (``session_manager.py:418/473``).
                # That is normal back-pressure — the project is at its
                # ``max_parallel_workers`` ceiling — not a server bug.
                # Surface it as ``429`` with a stable
                # ``worker_cap_exceeded`` code so clients can apply
                # back-off and operators see the same recovery story
                # the CLI emits.
                raise too_many_requests(
                    f"Worker cap exceeded for project "
                    f"{project_key}: {exc}",
                    code="worker_cap_exceeded",
                    hint=(
                        "Wait for an in-progress task on this "
                        "project to finish, raise "
                        "`max_parallel_workers` under "
                        f"`[projects.{project_key}]` in pollypm.toml, "
                        "or retry the claim once a slot frees up."
                    ),
                ) from exc
            task = svc.get(task_id)
            warnings = _collect_claim_warnings(svc, task, task_id)
            return _task_to_detail_with_plan(task, svc=svc), warnings
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "claim_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while claiming {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def _collect_claim_warnings(
    svc: object, task: object, task_id: str
) -> list[str]:
    """Build the ``TaskActionResult.warnings`` list for a successful claim.

    Three sources combine into one operator-facing list (#2064 round-10 + 11):

    - ``svc.last_provision_error`` — set by
      ``PgWorkService.claim`` (``pg_service.py:1905-1910``) when the
      DB transition committed but the per-task worker session
      failed to provision (tmux blew up, parallel-cap, worktree
      checkout, prompt write, etc). Round-11 refinement: that path
      can ALSO roll the task back to ``queued`` (cap-exceeded race;
      ``pg_service.py:1917-1939``). When the rollback fired the
      warning wording must say "rolled back" — saying "the DB claim
      is in effect" lies because the row is back to ``queued``.
    - ``svc._session_attach_error`` — set by
      :func:`pollypm.work.service_factory.attach_session_manager` when
      the SessionManager itself could not be wired (missing imports,
      StateStore failure, tmux client construction, etc). Without
      this surface the API would silently behave like a DB-only
      claim even though the operator expects the per-task worker
      lifecycle.

    Each entry uses the same "what to do" wording as the CLI warning
    at ``src/pollypm/work/cli.py:912-929`` so the recovery story is
    identical across surfaces. The list is empty on the happy path —
    clients can branch on ``len(warnings) > 0`` to decide whether to
    surface a banner.
    """
    warnings: list[str] = []
    last_provision_error = getattr(svc, "last_provision_error", None)
    session_attach_error = getattr(svc, "_session_attach_error", None)
    # Round-11 blocker #2: the claim path's post-commit rollback
    # (``pg_service.py:1917-1939``) sets ``last_provision_error`` AND
    # flips the row back to ``queued``. Inspect the post-claim
    # task to tell the operator the truth. ``work_status`` may be a
    # :class:`WorkStatus` enum or a string depending on whether the
    # caller passed a real pg ``Task`` or the FakeWorkService in
    # tests; normalise to the underlying value.
    status_attr = getattr(task, "work_status", None)
    status_value = getattr(status_attr, "value", status_attr)
    rolled_back = (
        bool(last_provision_error) and status_value == "queued"
    )
    if last_provision_error:
        if rolled_back:
            warnings.append(
                f"Worker session provisioning failed for {task_id}: "
                f"{last_provision_error}. The DB claim was rolled "
                f"back; the task is queued again so auto-claim can "
                f"retry. To recover: wait for the next claim sweep, "
                f"or raise `max_parallel_workers` under "
                f"`[projects.{_project_of(task_id)}]` in pollypm.toml "
                f"if back-pressure caused the rollback."
            )
        else:
            warnings.append(
                f"Worker session provisioning failed for {task_id}: "
                f"{last_provision_error}. The DB claim is in effect, "
                f"but no live agent lane was created. To recover: "
                f"either continue work from an existing worker "
                f"session for this project, or hold + resume to "
                f"retry provisioning "
                f"(`pm task hold {task_id} --reason 'provision "
                f"failed'` then `pm task resume {task_id}`)."
            )
    if session_attach_error:
        warnings.append(
            f"SessionManager wire-up failed for {task_id}: "
            f"{session_attach_error}. The DB claim is in effect, "
            f"but the worker-session subsystem could not be "
            f"initialised for this request — no per-task tmux lane "
            f"was provisioned. Check tmux availability and the "
            f"project worktree, then hold + resume the task to "
            f"retry (`pm task hold {task_id} --reason "
            f"'session attach failed'` then `pm task resume "
            f"{task_id}`)."
        )
    return warnings


def _project_of(task_id: str) -> str:
    """Best-effort project-key extraction for warning wording.

    Task IDs are ``"<project>/<n>"`` (see ``_parse_task_id`` in
    ``pollypm.work.pg_service``). Falls back to ``"<project>"`` if
    the shape is unexpected — the warning still renders, just with
    a placeholder. Used only for operator-facing messages, never
    for routing.
    """
    project, sep, _ = task_id.partition("/")
    return project if sep else "<project>"


def cancel_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str = "api",
    reason: str | None = None,
    force: bool = False,
) -> APITaskDetail:
    """Cancel a non-terminal task via the work-service.

    Mirrors ``pm work cancel``. ``reason`` is optional at the API
    surface (spec §5.3 body ``{reason?: str}``); the work-service's
    ``cancel`` requires a string, so we fall back to a generic message
    when the caller doesn't supply one — preserving the audit row's
    ``reason`` slot regardless.
    """
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
    )

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    task_id = f"{project_key}/{task_number}"
    cancel_reason = reason or "cancelled via API"
    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                before = svc.get(task_id)
                active_assignee = in_progress_assignee(before)
                if active_assignee and not force:
                    emit_cancel_safety_event(
                        before,
                        event=EVENT_TASK_CANCEL_WARNED,
                        actor=actor,
                        assignee=active_assignee,
                        project_path=project.path,
                        surface="api",
                        force=False,
                    )
                    raise APIError(
                        status_code=409,
                        code="confirmation_required",
                        message=(
                            f"Worker {active_assignee} is currently "
                            f"working task {task_id}."
                        ),
                        hint=(
                            "Re-issue the cancel request with "
                            "`?force=true` once the operator has "
                            "confirmed the active worker should be "
                            "interrupted."
                        ),
                    )
                svc.cancel(task_id, actor, cancel_reason)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                # Cancelling a terminal task is the most common path
                # here; spec §5.5 maps that to 409 ``invalid_state``.
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc) or f"Task {task_id} cannot be cancelled.",
                    hint="Tasks in terminal state (done/cancelled) cannot be cancelled again.",
                ) from exc
            task = svc.get(task_id)
            if active_assignee:
                emit_cancel_safety_event(
                    before,
                    event=EVENT_TASK_CANCEL_CONFIRMED,
                    actor=actor,
                    assignee=active_assignee,
                    project_path=project.path,
                    surface="api",
                    force=force,
                )
            return _task_to_detail_with_plan(task, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "cancel_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while cancelling {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def reopen_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str = "api",
    reason: str | None = None,
) -> APITaskDetail:
    """Reopen a cancelled task back to queued via the work-service."""
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
    )

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    task_id = f"{project_key}/{task_number}"
    reopen_reason = reason or "reopened via API"
    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                svc.reopen(task_id, actor, reopen_reason)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc) or f"Task {task_id} cannot be reopened.",
                    hint="Only cancelled tasks can be reopened.",
                ) from exc
            task = svc.get(task_id)
            return _task_to_detail_with_plan(task, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "reopen_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while reopening {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def reassign_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
) -> APITaskDetail:
    """Change the task's ``assignee`` via :meth:`WorkService.reassign_task`.

    Routes through the dedicated ``reassign_task`` work-service method
    (#2064 round-3) rather than ``svc.update(assignee=...)`` so the
    column write and the context-log breadcrumb commit in a single
    transaction. Spec §P-9 requires that mid-flight reassignment record
    a row like ``"worker reassigned from pete to nora"`` so the new
    owner can recover context via ``pm task get``. Using ``update``
    would update the column silently and break that invariant.
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
                # ``actor`` in the request body is the *new assignee*
                # (see :class:`TaskReassignRequest`). We attribute the
                # context-log entry to the API surface — the HTTP layer
                # is the operator of the handoff.
                task = svc.reassign_task(
                    task_id, new_assignee=actor, actor="api"
                )
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                # #2064 round-9 blocker #4: reassign now refuses
                # terminal / draft tasks (live-worker-swap invariant).
                # The work-service raises ``InvalidTransitionError``
                # from inside the row-lock select; surface as 409 so
                # the contract matches the ``/claim`` / ``/cancel``
                # transition endpoints.
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc)
                    or f"Task {task_id} cannot be reassigned in its "
                    f"current state.",
                    hint=(
                        "Reassign is a live-worker handoff; it refuses "
                        "draft (queue + claim first) and terminal "
                        "(done / cancelled) tasks."
                    ),
                ) from exc
            except WorkValidationError as exc:
                raise APIError(
                    status_code=422,
                    code="validation_error",
                    message=str(exc) or "Reassignment failed validation.",
                ) from exc
            return _task_to_detail_with_plan(task, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "reassign_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while reassigning {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


# ---------------------------------------------------------------------------
# Lifecycle REST verbs (#2137) — done / approve / hold / rework / block /
# review / in_progress. Each helper mirrors :func:`claim_task` /
# :func:`cancel_task`: open a fresh work-service, route to the matching
# pg-service method, map ``TaskNotFoundError`` → 404,
# ``InvalidTransitionError`` → 409 invalid_state, ``ValidationError`` →
# 422 validation_error, and ``_BACKING_STORE_ERRORS`` → 503. Returns the
# post-transition :class:`TaskDetail` so the client refreshes in one
# round-trip (spec §5.3 wrapper, same shape as the existing verbs).
# ---------------------------------------------------------------------------


def _run_lifecycle_transition(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    verb: str,
    op,
    *,
    with_session: bool = False,
) -> APITaskDetail:
    """Shared scaffolding for the #2137 lifecycle helpers.

    ``op(svc, task_id)`` performs the actual work-service call. The
    helper handles project lookup, exception mapping, and the final
    ``svc.get`` → :class:`TaskDetail` hydration so each verb's helper
    is two lines of intent + one ``op`` lambda.
    """
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
        ValidationError as WorkValidationError,
    )
    if with_session:
        from pollypm.work.service_factory import (
            create_work_service_with_session,
        )
    else:
        from pollypm.work.factory import create_work_service

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    task_id = f"{project_key}/{task_number}"
    try:
        if with_session:
            svc_cm = create_work_service_with_session(
                config=config,
                project_key=project_key,
                project_path=project.path,
            )
        else:
            svc_cm = create_work_service(
                config=config,
                project_key=project_key,
                project_path=project.path,
            )
        with svc_cm as svc:
            try:
                op(svc, task_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            except InvalidTransitionError as exc:
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc)
                    or f"Task {task_id} cannot transition via {verb}.",
                    hint=(
                        "Refresh the task to see the current "
                        "`work_status`; the requested transition is "
                        "not legal from that state."
                    ),
                ) from exc
            except WorkValidationError as exc:
                raise APIError(
                    status_code=422,
                    code="validation_error",
                    message=str(exc) or f"{verb} validation failed.",
                ) from exc
            task = svc.get(task_id)
            return _task_to_detail_with_plan(task, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "%s_task: backing store error for %s: %s",
            verb,
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while {verb}-ing {task_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


def done_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
) -> APITaskDetail:
    """Force a task to ``done`` via :meth:`PgWorkService.mark_done`.

    Operator "force done" gesture. The flow-respecting variant
    (:meth:`node_done`) requires a work-output payload; the Web UI
    needs a simpler surface (the Wave 2A finding behind #2137).
    """
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="done",
        op=lambda svc, task_id: svc.mark_done(task_id, actor),
    )


def approve_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    reason: str | None = None,
) -> APITaskDetail:
    """Approve a task at a review node — see :meth:`PgWorkService.approve`."""
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="approve",
        op=lambda svc, task_id: svc.approve(task_id, actor, reason),
    )


def hold_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    reason: str | None = None,
) -> APITaskDetail:
    """Move a task to ``on_hold`` — see :meth:`PgWorkService.hold`."""
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="hold",
        op=lambda svc, task_id: svc.hold(task_id, actor, reason),
    )


def rework_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    reason: str,
) -> APITaskDetail:
    """Reject a review-state task back to ``rework`` — see :meth:`PgWorkService.reject`.

    Note ``reason`` is required at the work-service level; the route
    model (:class:`TaskReworkRequest`) enforces ``min_length=1`` so a
    missing reason fails at request-validation time with 422.
    """
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="rework",
        op=lambda svc, task_id: svc.reject(task_id, actor, reason),
    )


def block_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    blocker_task_id: str,
) -> APITaskDetail:
    """Mark a task ``blocked`` by ``blocker_task_id`` — see :meth:`PgWorkService.block`."""
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="block",
        op=lambda svc, task_id: svc.block(task_id, actor, blocker_task_id),
    )


def review_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
) -> APITaskDetail:
    """Force ``in_progress`` → ``review`` — see :meth:`PgWorkService.force_review`."""
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="review",
        op=lambda svc, task_id: svc.force_review(task_id, actor),
    )


def in_progress_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
) -> APITaskDetail:
    """Force a non-terminal task into ``in_progress`` — see :meth:`PgWorkService.force_in_progress`."""
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="in_progress",
        op=lambda svc, task_id: svc.force_in_progress(task_id, actor),
    )


def release_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str,
    reason: str | None = None,
) -> APITaskDetail:
    """Release ``in_progress`` / ``rework`` back to ``queued``."""
    release_reason = reason or "released via API"
    return _run_lifecycle_transition(
        config,
        project_key,
        task_number,
        verb="release",
        op=lambda svc, task_id: svc.release(
            task_id, actor, release_reason
        ),
        with_session=True,
    )


# Labels supported on PATCH ``status`` — we route the request through the
# work-service lifecycle method that maps to each value. Statuses without
# a direct setter (e.g. ``in_progress``, ``review``) raise 422 with a
# pointer to the dedicated transition endpoint. Spec §5.4 frames PATCH as
# "selective field updates"; status-as-transition is the safe interpretation.
_PATCH_STATUS_TO_METHOD: dict[str, str] = {
    "queued": "queue",
    "cancelled": "cancel",
}


def patch_task(
    config: PollyPMConfig,
    project_key: str,
    task_number: int,
    *,
    actor: str = "api",
    labels: list[str] | None = None,
    status: str | None = None,
    metadata: dict[str, str] | None = None,
) -> APITaskDetail:
    """Selective edits — labels / status / metadata.

    Atomicity contract (#2064 round-2): the route layer refuses any
    request that combines ``status`` with ``labels`` / ``metadata``
    (returns ``400 invalid_request``) BEFORE this helper is called.
    That means each invocation here is one of two shapes:

    1. **Status-only PATCH** → routed through the matching lifecycle
       method (``svc.queue`` / ``svc.cancel``). A single work-service
       call, single DB write — naturally atomic.
    2. **Field-only PATCH** (labels and/or metadata) → one
       ``svc.update(...)`` call, batched into one ``UPDATE`` row
       statement by ``PgWorkService.update`` — naturally atomic.

    The previous in-memory preflight that gated labels+status across
    two service calls was racy under concurrent writers (the task's
    status could flip between preflight and the lifecycle call,
    leaving labels committed and the status write 409'ing). The
    narrowed contract above removes the racy code path entirely.

    ``status`` is mapped to the matching lifecycle method (``queue``,
    ``cancel``) when supported; everything else (in_progress /
    review / on_hold / etc.) returns 422 with a hint to the dedicated
    transition endpoint. ``metadata`` is stored as ``external_refs``
    (the existing free-form per-task key/value surface).
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
    # #2064 round-13: source the lifecycle state set from the canonical
    # ``WorkStatus`` enum instead of hand-copying it here. Adding /
    # renaming a state in ``pollypm.work.models`` should not also
    # require touching the HTTP layer to keep PATCH validation in sync.
    from pollypm.work.models import WorkStatus

    valid_statuses = {s.value for s in WorkStatus}
    if status is not None and status not in valid_statuses:
        raise APIError(
            status_code=422,
            code="validation_error",
            message=f"Unknown status: {status!r}",
            hint=(
                "Valid statuses: " + ", ".join(sorted(valid_statuses))
            ),
        )
    # Reject unsupported PATCH status targets BEFORE opening a writer.
    if status is not None and status not in _PATCH_STATUS_TO_METHOD:
        raise APIError(
            status_code=422,
            code="validation_error",
            message=(
                f"PATCH cannot set status={status!r}; this state "
                "is only reachable via a flow transition."
            ),
            hint=(
                "Use the dedicated endpoint (e.g. /claim, "
                "/approve-plan) or POST a context-aware "
                "transition instead of PATCH."
            ),
        )

    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            # #2064 round-13: the previous existence probe here caught
            # ``Exception`` and re-raised as ``not_found``, masking
            # ``_row_to_task`` bugs, facade misconfigurations, and other
            # 500-class failures as a benign-looking 404. The probe is
            # gone; each downstream call surfaces its own
            # ``TaskNotFoundError`` (mapped to 404) and lets every other
            # exception propagate to the existing 503/500 handlers.
            # Empty-PATCH still 404s via the unconditional ``svc.get``
            # at the end of the block.

            # Field-only PATCH: labels and/or metadata land in ONE
            # ``svc.update(...)`` call so they share a single DB
            # transaction. ``PgWorkService.update`` batches set-clauses
            # into a single UPDATE; combining the fields here prevents
            # any labels-commit-then-metadata-fails partial-write
            # window. Cannot coexist with ``status`` (route-layer 400).
            combined_fields: dict[str, object] = {}
            if labels is not None:
                combined_fields["labels"] = labels
            if metadata is not None:
                combined_fields["external_refs"] = metadata
            if combined_fields:
                try:
                    svc.update(task_id, **combined_fields)
                except TaskNotFoundError as exc:
                    raise not_found(f"Task not found: {task_id}") from exc
                except WorkValidationError as exc:
                    raise APIError(
                        status_code=422,
                        code="validation_error",
                        message=(
                            str(exc)
                            or "labels/metadata update failed validation."
                        ),
                    ) from exc

            # Status-only PATCH: route through the lifecycle owner
            # (``svc.queue`` / ``svc.cancel``). One service call, one
            # DB write — atomic. The route layer guarantees this
            # branch never coexists with a labels/metadata write.
            if status is not None:
                method_name = _PATCH_STATUS_TO_METHOD[status]
                try:
                    if method_name == "queue":
                        svc.queue(task_id, actor)
                    elif method_name == "cancel":
                        before = svc.get(task_id)
                        active_assignee = in_progress_assignee(before)
                        if active_assignee:
                            emit_cancel_safety_event(
                                before,
                                event=EVENT_TASK_CANCEL_WARNED,
                                actor=actor,
                                assignee=active_assignee,
                                project_path=project.path,
                                surface="api",
                                force=False,
                            )
                            raise APIError(
                                status_code=409,
                                code="confirmation_required",
                                message=(
                                    f"Worker {active_assignee} is currently "
                                    f"working task {task_id}."
                                ),
                                hint=(
                                    "Use POST /tasks/{project}/{n}/cancel"
                                    "?force=true after confirming the "
                                    "active worker should be interrupted."
                                ),
                            )
                        svc.cancel(task_id, actor, "patched via API")
                except TaskNotFoundError as exc:
                    raise not_found(f"Task not found: {task_id}") from exc
                except InvalidTransitionError as exc:
                    raise APIError(
                        status_code=409,
                        code="invalid_state",
                        message=(
                            str(exc)
                            or f"Status transition to {status!r} refused."
                        ),
                    ) from exc
                except WorkValidationError as exc:
                    raise APIError(
                        status_code=422,
                        code="validation_error",
                        message=str(exc) or "status transition gate failed.",
                    ) from exc

            # Final read also serves as the existence probe for an
            # all-None PATCH (route layer already rejects truly-empty
            # PATCH bodies, but a body with nothing matching the
            # accepted fields would otherwise sail through silently).
            try:
                task = svc.get(task_id)
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task_id}") from exc
            return _task_to_detail_with_plan(task, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "patch_task: backing store error for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while patching {task_id}",
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


def _inbox_already_archived(item_id: str, status: str) -> APIError:
    return APIError(
        status_code=409,
        code="invalid_state",
        message=f"Inbox item {item_id} is already {status}; cannot archive.",
        hint="Items in a terminal state cannot be re-archived.",
    )


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
                status = getattr(
                    src_task.work_status, "value", str(src_task.work_status)
                )
                if (
                    status in _ALREADY_ARCHIVED_STATUSES
                    and is_inbox_task_identity(src_task, svc)
                ):
                    raise _inbox_already_archived(item_id, status)
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
                message = str(exc)
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=message or (
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
            task = _active_plan_task_for_project(svc, project_key)
            plan = _build_plan(svc, task) if task is not None else None
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


def _active_plan_task_for_project(svc, project_key: str):
    try:
        review_tasks = svc.list_tasks(project=project_key, work_status="review")
    except Exception:  # noqa: BLE001
        review_tasks = []
    return _active_plan_task_from_review_tasks(review_tasks)


def _active_plan_task_from_review_tasks(review_tasks: list[object]):
    candidates = [
        t for t in review_tasks
        if _is_plan_task(t)
        and (getattr(t, "current_node_id", "") or "") == "user_approval"
    ]
    if not candidates:
        return None
    # Newest plan_version wins; updated_at breaks ties so repeated
    # review tasks in the same project do not return a stale decision.
    def _plan_sort_key(task) -> tuple[int, str]:
        updated = getattr(task, "updated_at", None)
        updated_key = (
            updated.isoformat()
            if hasattr(updated, "isoformat")
            else str(updated or "")
        )
        return (getattr(task, "plan_version", 1) or 1, updated_key)

    candidates.sort(key=_plan_sort_key, reverse=True)
    return candidates[0]


def _active_plan_from_review_tasks(svc, review_tasks: list[object]) -> APIPlan | None:
    task = _active_plan_task_from_review_tasks(review_tasks)
    return _build_plan(svc, task) if task is not None else None


def _active_plan_for_project(svc, project_key: str) -> APIPlan | None:
    task = _active_plan_task_for_project(svc, project_key)
    return _build_plan(svc, task) if task is not None else None


def approve_active_plan(
    config: PollyPMConfig,
    project_key: str,
    *,
    actor: str = "user",
    note: str | None = None,
) -> APITaskDetail:
    """Approve the active plan-review task for ``project_key``."""
    return _decide_active_plan(
        config,
        project_key,
        actor=actor,
        decision="approve",
        reason=note,
    )


def reject_active_plan(
    config: PollyPMConfig,
    project_key: str,
    *,
    actor: str = "user",
    reason: str,
) -> APITaskDetail:
    """Reject the active plan-review task for ``project_key``."""
    return _decide_active_plan(
        config,
        project_key,
        actor=actor,
        decision="reject",
        reason=reason,
    )


def _decide_active_plan(
    config: PollyPMConfig,
    project_key: str,
    *,
    actor: str,
    decision: str,
    reason: str | None,
) -> APITaskDetail:
    from pollypm.work.factory import create_work_service
    from pollypm.work.service_support import (
        InvalidTransitionError,
        TaskNotFoundError,
        ValidationError as WorkValidationError,
    )

    project = config.projects.get(project_key)
    if project is None:
        raise not_found(f"Project not registered: {project_key}")

    try:
        with create_work_service(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            task = _active_plan_task_for_project(svc, project_key)
            if task is None:
                raise not_found(
                    "No plan in review for this project",
                    hint=(
                        "Plans can be approved or rejected only while a "
                        "plan_project task is parked at the user_approval node."
                    ),
                )
            try:
                if decision == "approve":
                    updated = svc.approve(task.task_id, actor, reason)
                else:
                    updated = svc.reject(task.task_id, actor, reason or "")
            except TaskNotFoundError as exc:
                raise not_found(f"Task not found: {task.task_id}") from exc
            except InvalidTransitionError as exc:
                raise APIError(
                    status_code=409,
                    code="invalid_state",
                    message=str(exc) or f"Plan {task.task_id} is not reviewable.",
                    hint=(
                        "Refresh the project plan; only an active "
                        "user_approval review can be decided."
                    ),
                ) from exc
            except WorkValidationError as exc:
                raise APIError(
                    status_code=422,
                    code="validation_error",
                    message=str(exc) or f"Plan decision rejected for {task.task_id}.",
                    hint=(
                        "Use an authorized human actor such as `user`, "
                        "and include a rejection reason when rejecting."
                    ),
                ) from exc
            return _task_to_detail_with_plan(updated, svc=svc)
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "plan decision: backing store error for %s: %s",
            project_key,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable for project {project_key}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc


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
    timing = _task_timing_fields(task)
    return APITaskSummary(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        work_status=_enum_value(task.work_status),
        type=_enum_value(task.type),
        priority=_enum_value(task.priority),
        assignee=task.assignee,
        claimed_by_session=getattr(task, "claimed_by_session", None),
        current_node_id=task.current_node_id,
        plan_version=getattr(task, "plan_version", None),
        created_at=timing["created_at"],
        state_entered_at=timing["state_entered_at"],
        dwell_seconds=timing["dwell_seconds"],
        age_seconds=timing["age_seconds"],
        updated_at=getattr(task, "updated_at", None),
    )


def _task_summary_projection_to_api(row: TaskSummaryProjection) -> APITaskSummary:
    created_at = _as_aware_datetime(row.created_at)
    state_entered_at = _as_aware_datetime(row.state_entered_at) or created_at
    now = datetime.now(timezone.utc)
    return APITaskSummary(
        task_id=row.task_id,
        project=row.project,
        task_number=row.task_number,
        title=row.title,
        work_status=row.work_status,
        type=row.type,
        priority=row.priority,
        assignee=row.assignee,
        claimed_by_session=row.claimed_by_session,
        current_node_id=row.current_node_id,
        plan_version=row.plan_version,
        created_at=created_at,
        state_entered_at=state_entered_at,
        dwell_seconds=_elapsed_seconds(state_entered_at, now),
        age_seconds=_elapsed_seconds(created_at, now),
        updated_at=_as_aware_datetime(row.updated_at),
    )


def _task_timing_fields(task) -> dict[str, datetime | int | None]:
    created_at = _as_aware_datetime(getattr(task, "created_at", None))
    state_entered_at = _state_entered_at(task)
    now = datetime.now(timezone.utc)
    dwell_seconds = _elapsed_seconds(state_entered_at, now)
    age_seconds = _elapsed_seconds(created_at, now)
    return {
        "created_at": created_at,
        "state_entered_at": state_entered_at,
        "dwell_seconds": dwell_seconds,
        "age_seconds": age_seconds,
    }


def _state_entered_at(task) -> datetime | None:
    current = _enum_value(getattr(task, "work_status", ""))
    transitions = list(getattr(task, "transitions", None) or [])
    for transition in reversed(transitions):
        if _enum_value(getattr(transition, "to_state", "")) == current:
            entered = _as_aware_datetime(getattr(transition, "timestamp", None))
            if entered is not None:
                return entered
    return _as_aware_datetime(getattr(task, "created_at", None))


def _as_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_seconds(start: datetime | None, end: datetime) -> int | None:
    if start is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _task_to_detail_with_plan(task, *, svc) -> APITaskDetail:
    """Return :class:`APITaskDetail` with ``plan`` hydrated for plan reviews.

    Mirrors the rule applied at :func:`get_task_detail` (the canonical
    GET-shape builder) and :func:`queue_task`: if the task is a plan
    task currently in review, build the ``APIPlan`` payload from the
    same ``_build_plan`` helper the GET path uses. Any mutation helper
    that returns a :class:`TaskActionResult` (claim / reassign / patch
    / queue / cancel) MUST route through this helper so the response's
    ``task`` field carries the same shape as a follow-up GET — the
    ``TaskActionResult`` envelope is documented as the
    refresh-without-follow-up-GET contract (#2064 round-4, spec §5.3,
    ``src/pollypm/web_api/models.py:347``).

    Plan hydration is best-effort: ``_build_plan`` exceptions collapse
    to ``plan=None`` rather than 500'ing the mutation, matching the
    long-standing GET behaviour.
    """
    plan: APIPlan | None = None
    if _is_plan_task(task) and _is_in_review(task):
        try:
            plan = _build_plan(svc, task)
        except Exception:  # noqa: BLE001
            plan = None
    return _task_to_detail(task, plan=plan)


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
    summary = _task_to_summary(task)
    return APITaskDetail(
        **summary.model_dump(),
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
) -> InboxListPage:
    """Aggregate inbox view across one or all projects.

    Mirrors :func:`pollypm.cockpit_inbox.render_inbox_panel` at a
    coarser granularity — the API only exposes the typed shape per
    spec §7. Projects with no inbox state contribute nothing.
    """
    # Preserve the first-page hot path from #2205: fetch only enough
    # candidates to render the page plus one sentinel, then run the
    # full scan separately for exact metadata fields required by the
    # response contract (#2231). Cursor pages already require the full
    # ordered set to locate the cursor.
    candidate_limit = None if cursor is not None else limit + 1
    items, candidate_unread_count, candidate_complete = _collect_inbox_items(
        config,
        project=project,
        type_filter=type_filter,
        state_filter=state_filter,
        limit=candidate_limit,
    )
    if candidate_limit is None or candidate_complete:
        metric_items = items
        unread_count = candidate_unread_count
    else:
        metric_items, unread_count, _ = _collect_inbox_items(
            config,
            project=project,
            type_filter=type_filter,
            state_filter=state_filter,
            limit=None,
        )
    items.sort(key=lambda item: item.updated_at, reverse=True)

    cursor_idx = 0
    if cursor is not None:
        for idx, item in enumerate(items):
            if item.id == cursor:
                cursor_idx = idx + 1
                break
    total = len(metric_items)
    page = items[cursor_idx : cursor_idx + limit]
    next_cursor: str | None = None
    if cursor_idx + limit < len(items) and page:
        next_cursor = page[-1].id
    return InboxListPage(
        items=page,
        next_cursor=next_cursor,
        total=total,
        has_more=next_cursor is not None,
        unread_count=unread_count,
    )


def get_inbox_item(config: PollyPMConfig, item_id: str) -> APIInboxItemDetail | None:
    if "/" not in item_id:
        return None
    project_key = item_id.split("/", 1)[0]
    project = config.projects.get(project_key)
    if project is None:
        return None
    try:
        from pollypm.work.service_support import TaskNotFoundError

        with _open_work_service_readonly(
            config=config, project_key=project_key, project_path=project.path
        ) as svc:
            try:
                task = svc.get(item_id)
            except TaskNotFoundError:
                return None
            target = _task_to_inbox_item(
                task, svc, flow_cache={}, include_closed=True,
            )
    except _BACKING_STORE_ERRORS as exc:
        logger.warning(
            "get_inbox_item: backing store error for %s: %s",
            item_id,
            exc,
            exc_info=True,
        )
        raise service_unavailable(
            f"Backing store unavailable while reading {item_id}",
            hint="Retry shortly; check `pm doctor` if the failure persists.",
        ) from exc
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
    config: PollyPMConfig,
    *,
    project: str | None,
    type_filter: str | None = None,
    state_filter: str | None = None,
    limit: int | None = None,
) -> tuple[list[APIInboxItem], int, bool]:
    """Load inbox items from the work-service for the requested projects.

    "Inbox" here = chat-flow tasks + plan-review tasks. Mirrors the
    set the cockpit panel surfaces. Each task becomes one inbox
    entry; ``id`` is the task_id so later detail / reply paths can
    address it.
    """
    out: list[APIInboxItem] = []
    unread_count = 0
    complete = True
    keys: Iterable[str]
    if project is not None:
        keys = (project,) if project in config.projects else ()
    else:
        keys = config.projects.keys()

    now = datetime.now(timezone.utc)
    include_closed = _state_filter_requests_closed(state_filter)
    for key in keys:
        proj = config.projects[key]
        try:
            with _open_work_service_readonly(
                config=config, project_key=key, project_path=proj.path
            ) as svc:
                tasks = _list_inbox_candidate_tasks(
                    svc,
                    project=key,
                    type_filter=type_filter,
                    state_filter=state_filter,
                    limit=limit,
                )
                if limit is not None and len(tasks) >= limit:
                    complete = False
                read_marker_numbers = _task_numbers_with_context_entry(
                    svc, project=key, entry_type="read", tasks=tasks,
                )
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
                    if not inbox_task_matches_type(task, type_filter):
                        continue
                    entry = _task_to_inbox_item(
                        task,
                        svc,
                        flow_cache=flow_cache,
                        include_closed=include_closed,
                    )
                    if entry is None:
                        continue
                    if not _inbox_item_matches_state(entry, state_filter):
                        continue
                    out.append(entry)
                    if _task_number(task) not in read_marker_numbers:
                        unread_count += 1
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
    return out, unread_count, complete


def _task_number(task) -> int:
    value = getattr(task, "task_number", None)
    if value is not None:
        return int(value)
    return int(str(getattr(task, "task_id")).split("/", 1)[1])


def _task_numbers_with_context_entry(
    svc,
    *,
    project: str,
    entry_type: str,
    tasks,
) -> set[int]:
    bulk = getattr(svc, "task_numbers_with_context_entry", None)
    if callable(bulk):
        return set(bulk(project=project, entry_type=entry_type))

    marked: set[int] = set()
    for task in tasks:
        try:
            entries = svc.get_context(
                task.task_id, entry_type=entry_type, limit=1,
            )
        except Exception:  # noqa: BLE001 - metrics should not hide inbox rows
            logger.debug(
                "inbox: context marker lookup failed for %s",
                getattr(task, "task_id", "<unknown>"),
                exc_info=True,
            )
            continue
        if entries:
            marked.add(_task_number(task))
    return marked


_CLOSED_INBOX_STATE_FILTERS: frozenset[str] = frozenset(
    {"closed", "resolved", "archived"}
)


def _state_filter_requests_closed(state_filter: str | None) -> bool:
    return (
        state_filter is not None
        and str(state_filter).strip().lower() in _CLOSED_INBOX_STATE_FILTERS
    )


def _inbox_item_matches_state(
    item: APIInboxItem, state_filter: str | None,
) -> bool:
    if state_filter is None:
        return item.state != "closed"
    wanted = str(state_filter).strip().lower()
    if wanted in _CLOSED_INBOX_STATE_FILTERS:
        return item.state == "closed"
    return item.state == wanted


def _list_inbox_candidate_tasks(
    svc,
    *,
    project: str,
    type_filter: str | None,
    state_filter: str | None,
    limit: int | None,
):
    """Ask the work-service for a bounded inbox-candidate page.

    Pg exposes ``list_inbox_candidate_tasks`` so state/type predicates and
    limits land in SQL. Older fakes/backends fall back to ``list_tasks`` with
    the same limit when their signature accepts it.
    """
    optimized = getattr(svc, "list_inbox_candidate_tasks", None)
    if callable(optimized):
        return optimized(
            project=project,
            type_filter=type_filter,
            state_filter=state_filter,
            limit=limit,
        )

    kwargs: dict[str, object] = {"project": project}
    if limit is not None:
        kwargs["limit"] = limit
    try:
        return svc.list_tasks(**kwargs)
    except TypeError:
        kwargs.pop("limit", None)
        return svc.list_tasks(**kwargs)


# ---------------------------------------------------------------------------
# Snooze visibility helpers (#2060)
#
# The POST /inbox/{id}/snooze endpoint persists an ``entry_type='snooze'``
# row whose text starts with ``until_iso=<ISO>; snoozed until <ISO>``.
# The "still snoozed?" predicate lives in
# ``pollypm.work.inbox_snooze`` so cockpit can adopt them WITHOUT
# duplicating regex logic (Codex round-2 ask on PR #2060). The import
# alias keeps the existing call sites + tests working unchanged.
#
# ``_active_snoozed_ids`` calls ``svc.latest_snoozes_bulk(...)``
# (single SQL on pg) instead of the original per-task
# ``svc.get_context(entry_type='snooze', limit=1)`` loop — the
# inbox-list path is user-facing and a 50-task page was paying N
# round-trips to the work-service per request.
# ---------------------------------------------------------------------------

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


def _task_to_inbox_item(
    task,
    svc=None,
    flow_cache=None,
    *,
    include_closed: bool = False,
) -> APIInboxItem | None:
    is_member = _is_inbox_member(task, svc, flow_cache=flow_cache)
    if not is_member:
        if not include_closed:
            return None
        flow_lookup = svc if svc is not None else _NoFlowLookup()
        if not is_archived_inbox_task(
            task, flow_lookup, flow_cache=flow_cache,
        ):
            return None
    state = _inbox_state_from_task(task)
    if state == "closed" and not include_closed:
        return None
    labels = [str(lbl) for lbl in (getattr(task, "labels", []) or [])]
    # Exact-equality on ``plan_review`` (NOT substring) so labels like
    # ``not_plan_review`` / ``planning`` cannot get classified as
    # ``type=plan_review`` items. Mirrors the canonical
    # :func:`pollypm.work.inbox_view._is_plan_review_label` predicate
    # the write gate uses — Codex round-6 blocker 3 on PR #2060.
    is_plan_review = any(lbl == "plan_review" for lbl in labels)
    item_type = inbox_item_type_for_task(task)
    body = task.description or ""
    preview = body.strip().splitlines()[0] if body.strip() else None
    metadata: dict[str, Any] = {
        "task_id": task.task_id,
        "labels": labels,
        "flow_template_id": task.flow_template_id,
        "kind": getattr(getattr(task, "kind", None), "value", None)
        or str(getattr(task, "kind", "legacy") or "legacy"),
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
    return inbox_state_for_task(task)


def _inbox_owner_for_task(task) -> str:
    roles = getattr(task, "roles", {}) or {}
    operator = str(roles.get("operator", "")).lower()
    if operator in {"pm", "pa", "worker", "operator"}:
        return operator
    if "architect" in operator:
        return "pm"
    return "pm"


def _load_inbox_messages(config: PollyPMConfig, item: APIInboxItem) -> list[APIInboxMessage]:
    """Pull reply context entries that drive an inbox thread."""
    project = config.projects.get(item.project)
    if project is None:
        return []

    out: list[APIInboxMessage] = []
    try:
        with _open_work_service_readonly(
            config=config, project_key=item.project, project_path=project.path
        ) as svc:
            list_replies = getattr(svc, "list_replies", None)
            if callable(list_replies):
                entries = list_replies(item.id) or []
            else:
                entries = svc.get_context(item.id, entry_type="reply") or []
                entries = list(reversed(entries))
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
    "cancel_task",
    "claim_task",
    "get_active_plan",
    "get_inbox_item",
    "get_project",
    "get_task_detail",
    "init_project_guide_for_role",
    "list_all_tasks",
    "list_inbox",
    "list_project_tasks",
    "list_projects",
    "load_api_config",
    "mark_read_inbox_item",
    "patch_task",
    "project_drilldown",
    "promote_inbox_to_task",
    "queue_task",
    "reassign_task",
    "reopen_task",
    "reply_inbox_item",
    "set_project_tracked",
    "snooze_inbox_item",
    "StaleCursorError",
]
