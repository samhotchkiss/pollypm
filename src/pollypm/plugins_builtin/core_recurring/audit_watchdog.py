"""``audit.watchdog`` cadence handler — surface forensic findings.

Born from the savethenovel post-mortem (2026-05-06): the audit log
(#1342) records every task / marker mutation; this handler reads
those events on a fixed cadence and surfaces "broken state" findings
via the existing ``upsert_alert`` channel — the same surface used by
``blocked_chain.sweep`` (#1073) and the supervisor's session-health
sweep. No new alert channel is introduced.

The handler is a thin wrapper around :mod:`pollypm.audit.watchdog`,
which holds the pure detectors. The heartbeat scheduler invokes
``audit_watchdog_handler`` every 5 minutes; per-tick behaviour:

1. Emit a ``heartbeat.tick`` audit event so liveness is observable.
2. For each known project, read the last ``window_seconds * 2`` of
   audit events and run the four detectors.
3. For every finding, emit an ``audit.finding`` audit event AND
   upsert an alert keyed by ``(rule, project, subject)``.

Idempotent across ticks because :meth:`upsert_alert` collapses
repeats. The emitted ``audit.finding`` events are deliberately
duplicated each tick — the audit log is append-only and operators
want to see "this fired N ticks in a row" as a signal of severity.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pollypm.audit.watchdog import (
    ESCALATION_THROTTLE_SECONDS,
    OPERATOR_DISPATCH_THROTTLE_SECONDS,
    Finding,
    RULE_DUPLICATE_ADVISOR_TASKS,
    RULE_LEGACY_DB_SHADOW,
    RULE_PLAN_MISSING_ALERT_CHURN,
    RULE_PLAN_REVIEW_MISSING,
    RULE_QUEUE_WITHOUT_MOTION,
    RULE_REJECTION_LOOP,
    RULE_ROLE_SESSION_MISSING,
    RULE_STATE_DB_MISSING,
    RULE_STUCK_DRAFT,
    RULE_TASK_ON_HOLD_STALE,
    RULE_TASK_PROGRESS_STALE,
    RULE_TASK_REVIEW_STALE,
    RULE_WORKER_SESSION_DEAD_LOOP,
    TIER_1,
    TIER_2,
    TIER_3,
    WATCHDOG_ALERT_TYPE,
    WatchdogConfig,
    emit_escalation_dispatched,
    emit_finding,
    emit_heartbeat_tick,
    emit_operator_dispatched,
    format_finding_message,
    format_unstick_brief,
    scan_project,
    tier_handoff_prompt,
    was_recently_dispatched,
    was_recently_operator_dispatched,
    watchdog_alert_session_name,
)


@dataclass(slots=True, frozen=True)
class _LegacyDbShadow:
    """One project's canonical-vs-legacy DB shadow descriptor (#1519).

    Used by :func:`_gather_legacy_db_shadows` to feed the pure
    ``legacy_db_shadow`` detector. Carries enough context that the
    detector can build a useful Finding and the self-heal action can
    target the right project without re-walking the registry.
    """

    project_key: str
    project_path: Path
    canonical_db: Path
    legacy_db: Path
    canonical_row_count: int
    legacy_row_count: int


@dataclass(slots=True, frozen=True)
class _StateDbProbe:
    """One project's canonical-state.db presence probe (#1546 tier-1).

    Used by :func:`_gather_state_db_probes` to feed the pure
    ``state_db_missing`` detector. Post-#339 / #1004 the canonical
    work DB is ``<workspace_root>/.pollypm/state.db`` — a single
    workspace-scope file shared across all projects. ``canonical_present``
    therefore reflects workspace-DB presence, not the deprecated
    ``<project>/.pollypm/state.db`` location (a healthy project after
    the workspace-root migration has no per-project DB and that's the
    expected shape, so the pre-#1546 detection rule fired on every tick).

    ``legacy_archives_present`` is True iff one or more
    ``<workspace_root>/.pollypm/state.db.legacy-*`` files exist (the
    post-archival shape that distinguishes "the canonical DB was
    archived away" from "this workspace never had a DB"). The self-heal
    action re-creates the workspace DB by opening it through the work
    service so the schema replay rebuilds the tables.
    """

    project_key: str
    project_path: Path
    canonical_present: bool
    legacy_archives_present: bool
    canonical_db_path: Path | None = None


@dataclass(slots=True, frozen=True)
class _PlanMissingClear:
    """One ``alert.cleared`` event for a ``plan_missing`` alert (#1524).

    Used by :func:`_gather_plan_missing_clears` to feed the pure
    ``plan_missing_alert_churn`` detector. ``scope`` mirrors the
    synthetic ``plan_gate-<project>`` scope the sweep handler uses on
    the alert; ``cleared_at`` is the message row's ``created_at``
    timestamp (i.e. when the clear event was recorded).
    """

    scope: str
    cleared_at: datetime


# #1414 — only the auto-unstick rules are eligible for architect dispatch.
# Legacy forensic rules (orphan_marker, marker_leaked, etc.) already have
# operator-actionable alerts and predate this dispatch path; we leave
# them additive-only to keep the blast radius small.
#
# #1546 — ``role_session_missing`` moved out (now tier-1 self-heal,
# spawns the missing lane). ``rejection_loop`` lands here as a tier-1
# detector → tier-2 dispatch: the project PM receives structured
# evidence and decides whether to retry, restructure, or escalate.
_DISPATCHABLE_RULES: frozenset[str] = frozenset({
    # #1440 — stale drafts are architect-originated planning stalls. Alerting
    # alone leaves them parked forever; dispatch the architect to queue,
    # cancel, or rewrite them.
    RULE_STUCK_DRAFT,
    RULE_TASK_REVIEW_STALE,
    RULE_TASK_PROGRESS_STALE,
    RULE_WORKER_SESSION_DEAD_LOOP,
    # #1424 — on_hold escalation lands in the architect's pane with the
    # reviewer's rationale folded into the brief. Default first responder
    # is the architect; only the architect calls ``pm notify`` if the
    # issue genuinely needs human judgement.
    RULE_TASK_ON_HOLD_STALE,
    # #1546 — rejection-loop detector → tier-2 PM dispatch.
    RULE_REJECTION_LOOP,
})

# #1546 — rules whose finding routes to the operator (tier-3) leg by
# default. ``queue_without_motion`` is the safety-net probe; the queue
# stalling out is a cross-cutting failure that frequently outlives a
# tier-2 PM dispatch, so the cadence handler routes it directly to the
# operator inbox via ``_maybe_dispatch_to_operator``.
_OPERATOR_DISPATCHABLE_RULES: frozenset[str] = frozenset({
    RULE_QUEUE_WITHOUT_MOTION,
})


logger = logging.getLogger(__name__)


__all__ = [
    "audit_watchdog_handler",
    "AUDIT_WATCHDOG_HANDLER_NAME",
    "AUDIT_WATCHDOG_SCHEDULE",
]


AUDIT_WATCHDOG_HANDLER_NAME = "audit.watchdog"
# Every 5 minutes is the same cadence as ``stuck_claims.sweep`` /
# ``alerts.gc`` — enough to catch the savethenovel-class symptom
# within ~5 min of occurrence without spamming the queue.
AUDIT_WATCHDOG_SCHEDULE = "@every 5m"


def _route_to_alert_sink(
    finding: Finding,
    *,
    msg_store: Any,
    state_store: Any,
) -> bool:
    """Upsert a finding via the unified Store, falling back to StateStore.

    Mirrors ``blocked_chain._emit_alert``: prefer the unified
    ``msg_store`` (post-#349 path) and fall back to the legacy
    ``StateStore`` so the cadence still works on installs that
    haven't migrated yet.
    """
    target = msg_store or state_store
    if target is None:
        return False
    upsert = getattr(target, "upsert_alert", None)
    if not callable(upsert):
        return False
    session_name = watchdog_alert_session_name(
        finding.rule, finding.project, finding.subject,
    )
    try:
        upsert(
            session_name,
            WATCHDOG_ALERT_TYPE,
            finding.severity,
            format_finding_message(finding),
        )
        return True
    except Exception:  # noqa: BLE001 — alert path failures must not crash the sweep
        logger.debug(
            "audit.watchdog: upsert_alert failed for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        return False


def _config_from_payload(payload: dict[str, Any]) -> WatchdogConfig:
    """Build a :class:`WatchdogConfig` from the handler payload."""
    if not isinstance(payload, dict):
        return WatchdogConfig()
    kwargs: dict[str, int] = {}
    for field_name in (
        "window_seconds",
        "stuck_draft_seconds",
        "cancel_grace_seconds",
        "review_stale_seconds",
        "progress_stale_seconds",
        "on_hold_stale_seconds",
    ):
        raw = payload.get(field_name)
        if raw is None:
            continue
        try:
            kwargs[field_name] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return WatchdogConfig(**kwargs) if kwargs else WatchdogConfig()


def _gather_done_plan_tasks(
    project_key: str, project_path: Path | None,
) -> list[Any]:
    """Best-effort load of recent ``done`` plan-shaped tasks for ``project_key``.

    Powers the ``plan_review_missing`` rule (#1511). Filters to:

    * ``work_status=done``
    * Plan-shaped labels (poc-plan / plan / project-plan)
    * Non-plan_project flow (``plan_project`` / ``critique_flow`` already
      emit plan_review through their reflection nodes)

    Returns an empty list on any failure — the watchdog rule then no-ops
    for that project, which is safer than firing false-positive findings.
    """
    try:
        from pollypm.work import create_work_service
        from pollypm.work.models import WorkStatus
        from pollypm.work.plan_review_emit import task_is_eligible_for_backstop
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: plan-review imports failed", exc_info=True,
        )
        return []
    try:
        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            list_fn = getattr(svc, "list_tasks", None)
            if not callable(list_fn):
                return []
            tasks = list(
                list_fn(work_status=WorkStatus.DONE.value, project=project_key)
            )
            return [task for task in tasks if task_is_eligible_for_backstop(task)]
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: done-plan-task load failed for %s",
            project_key, exc_info=True,
        )
        return []


def _make_plan_review_probe(project_key: str, project_path: Path | None):
    """Return a ``(project, plan_task_id) -> bool`` probe over the messages table.

    Resolves the canonical workspace state DB through the work-service
    factory (same path :func:`_gather_open_tasks` uses) so the probe
    queries the same ``messages`` table the cockpit inbox renders from.
    """
    try:
        from pollypm.work import create_work_service
        from pollypm.work.plan_review_emit import already_has_plan_review_message
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: plan-review probe imports failed", exc_info=True,
        )
        return None
    try:
        # Resolve the DB path via a short-lived service open. Avoids
        # plumbing the resolver directly here.
        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            db_path = getattr(svc, "_db_path", None)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: probe db_path resolve failed for %s",
            project_key, exc_info=True,
        )
        return None
    if db_path is None:
        return None
    resolved_db_path = Path(db_path)

    def _probe(project: str, plan_task_id: str) -> bool:
        return already_has_plan_review_message(
            db_path=resolved_db_path,
            project=project,
            plan_task_id=plan_task_id,
        )

    return _probe


def _backfill_plan_review_for_finding(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
) -> str | None:
    """Emit the missing plan_review row for a ``plan_review_missing`` finding.

    Best-effort — any failure is logged and swallowed. Returns the new
    inbox task id on success, ``None`` on skip / failure. Idempotent
    because :func:`emit_plan_review_for_task` re-checks the messages
    table before writing.
    """
    plan_task_id = (finding.metadata or {}).get("plan_task_id") or finding.subject
    if not plan_task_id:
        return None
    try:
        from pollypm.work import create_work_service
        from pollypm.work.plan_review_emit import emit_plan_review_for_task
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: backfill imports failed", exc_info=True,
        )
        return None
    try:
        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            try:
                task = svc.get(plan_task_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "audit.watchdog: get(%s) failed during plan_review backfill",
                    plan_task_id, exc_info=True,
                )
                return None
            if task is None:
                return None
            return emit_plan_review_for_task(
                svc=svc,
                task=task,
                actor="audit_watchdog",
                requester="user",
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: plan_review backfill failed for %s",
            plan_task_id, exc_info=True,
        )
        return None


def _gather_open_tasks(project_key: str, project_path: Path | None) -> list[Any]:
    """Best-effort load of in-flight tasks for state-based rules.

    Routes through :func:`pollypm.work.create_work_service` (the factory
    landed in #1389), which delegates path resolution to
    :func:`pollypm.work.db_resolver.resolve_work_db_path`. Post-#1004
    the canonical DB lives at ``<workspace_root>/.pollypm/state.db`` —
    a per-project ``<project>/.pollypm/state.db`` is at best stale and
    at worst empty (#1419), so we deliberately do NOT pass a hand-built
    ``db_path`` here. ``project_key`` is forwarded for resolver warnings
    and ``project_path`` for project-aware audit metadata only.

    Powers five rules: ``role_session_missing`` (since #1414),
    ``task_progress_stale`` (#1444), and the state-based variants of
    ``task_review_stale``, ``task_on_hold_stale``, and ``stuck_draft``
    (#1433). For tasks at the watched states
    (in_progress / review / on_hold / draft) we re-fetch via :meth:`get` so
    ``transitions`` are hydrated — :meth:`list_nonterminal_tasks` skips
    transition loading for non-plan-review tasks (it only includes
    history for plan-review labelled rows). Without transitions the
    state-based detectors fall back to ``updated_at``, which is touched
    on every status change and is therefore a noisy proxy.

    Returns an empty list on any failure — the watchdog keeps working
    even when the work-service is unreachable; the affected rules
    simply no-op for that project.
    """
    _STATE_RULE_STATES = frozenset({"in_progress", "review", "on_hold", "draft"})
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            list_fn = getattr(svc, "list_nonterminal_tasks", None)
            if not callable(list_fn):
                return []
            tasks = list(list_fn(project=project_key))
            # Hydrate transitions for tasks at the watched states so the
            # state-based detectors can compute "time in this state"
            # accurately. Best-effort; if ``svc.get`` blows up we fall
            # through to the un-hydrated row (the detector then falls
            # back to ``updated_at``, which is still better than nothing).
            get_fn = getattr(svc, "get", None)
            if callable(get_fn):
                hydrated: list[Any] = []
                for task in tasks:
                    status = getattr(task, "work_status", None)
                    status_value = getattr(status, "value", status)
                    if status_value in _STATE_RULE_STATES:
                        try:
                            project = getattr(task, "project", "")
                            task_number = getattr(task, "task_number", None)
                            if project and task_number is not None:
                                full = get_fn(f"{project}/{task_number}")
                                if full is not None:
                                    hydrated.append(full)
                                    continue
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "audit.watchdog: hydrate-transitions failed for %s/%s",
                                getattr(task, "project", "?"),
                                getattr(task, "task_number", "?"),
                                exc_info=True,
                            )
                    hydrated.append(task)
                return hydrated
            return tasks
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: open-task load failed for %s",
            project_key, exc_info=True,
        )
        return []


def _count_work_tasks_for_project(db_path: Path, project_key: str) -> int:
    """Return the number of ``work_tasks`` rows for ``project_key`` in ``db_path``.

    Best-effort; opens read-only and swallows every exception, returning
    ``0`` on any failure. We deliberately avoid going through the
    work-service factory so this probe doesn't pin a connection or
    trigger resolver migration side-effects — it's a pure peek.

    Powers the #1519 ``legacy_db_shadow`` detector. Returning 0 on a
    missing / unreadable DB means the detector treats that side as
    empty, which is the right default — we only fire when *both* sides
    are non-empty.
    """
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM work_tasks WHERE project = ?",
                (project_key,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            return 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _gather_legacy_db_shadows(
    *,
    services: Any,
) -> list[_LegacyDbShadow]:
    """Probe each known project for canonical/legacy DB shadowing (#1519).

    Walks ``services.known_projects`` once per cadence tick and returns
    one descriptor per project whose canonical workspace DB AND legacy
    per-project DB BOTH carry rows. The pure
    :func:`_detect_legacy_db_shadow` then emits findings; the cadence
    handler invokes :func:`_self_heal_legacy_db_shadow` to drain the
    legacy file via the existing
    :func:`migrate_legacy_per_project_dbs` helper (idempotent —
    rows already in canonical are skipped, source archived to
    ``state.db.legacy-1004`` only after every row was matched/copied).

    Returns an empty list if the workspace root can't be determined or
    if no projects are registered. Filesystem / sqlite failures for an
    individual project are swallowed and that project is skipped — the
    rule is additive ("alert only when we're sure"), not gated.
    """
    config = getattr(services, "config", None)
    if config is None:
        return []
    project_settings = getattr(config, "project", None)
    workspace_root_raw = getattr(project_settings, "workspace_root", None)
    if workspace_root_raw is None:
        return []
    try:
        workspace_root = Path(workspace_root_raw)
    except TypeError:
        return []
    canonical_db = workspace_root / ".pollypm" / "state.db"
    if not canonical_db.exists():
        # No canonical DB yet — nothing can shadow it. Returning empty
        # means we don't pre-emptively migrate a project before its
        # canonical DB is even populated.
        return []
    canonical_real: Path | None
    try:
        canonical_real = canonical_db.resolve()
    except OSError:
        canonical_real = None

    shadows: list[_LegacyDbShadow] = []
    known = getattr(services, "known_projects", None) or ()
    for project in known:
        project_key = getattr(project, "key", None) or getattr(
            project, "name", None,
        )
        project_path_raw = getattr(project, "path", None)
        if not project_key or project_path_raw is None:
            continue
        try:
            project_path = Path(project_path_raw)
        except TypeError:
            continue
        legacy_db = project_path / ".pollypm" / "state.db"
        if not legacy_db.exists():
            continue
        # Skip the project whose path *is* the workspace root — its
        # legacy DB IS the canonical DB; nothing to migrate.
        try:
            if canonical_real is not None and legacy_db.resolve() == canonical_real:
                continue
        except OSError:
            continue
        legacy_rows = _count_work_tasks_for_project(legacy_db, project_key)
        if legacy_rows <= 0:
            continue
        canonical_rows = _count_work_tasks_for_project(canonical_db, project_key)
        if canonical_rows <= 0:
            # Pure-legacy project — Part A's read-path fallback already
            # routes to legacy, so the dashboard renders correctly. We
            # could still migrate proactively here, but staying additive
            # keeps the action-side of this rule narrowly scoped to the
            # actual divergence (rows on both sides).
            continue
        shadows.append(_LegacyDbShadow(
            project_key=project_key,
            project_path=project_path,
            canonical_db=canonical_db,
            legacy_db=legacy_db,
            canonical_row_count=canonical_rows,
            legacy_row_count=legacy_rows,
        ))
    return shadows


def _gather_plan_missing_clears(
    *,
    services: Any,
    now: datetime,
    config: WatchdogConfig,
) -> list[_PlanMissingClear]:
    """Return one record per ``alert.cleared`` event for ``plan_missing``
    alerts inside the churn window.

    #1524 — feeds the pure
    :func:`pollypm.audit.watchdog._detect_plan_missing_alert_churn`
    detector. The clear lifecycle is recorded in the ``messages``
    table (see :meth:`SQLAlchemyStore.clear_alert`): one ``event`` row
    with ``subject='alert.cleared'`` and ``sender='plan_missing'`` per
    closed row. We query the messages table over the configured churn
    window and return one descriptor per match. The detector groups
    by scope and counts.

    Best-effort: any I/O failure returns an empty list so a transient
    DB hiccup doesn't fire a false-positive churn finding.
    """
    msg_store = getattr(services, "msg_store", None) or getattr(
        services, "state_store", None,
    )
    if msg_store is None:
        return []
    query = getattr(msg_store, "query_messages", None)
    if not callable(query):
        return []
    cutoff = now - timedelta(seconds=config.plan_missing_churn_window_seconds)
    try:
        rows = query(
            type="event",
            sender="plan_missing",
            since=cutoff,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: plan_missing-clear query failed", exc_info=True,
        )
        return []

    clears: list[_PlanMissingClear] = []
    for row in rows or ():
        # ``record_event`` stamps subject='alert.cleared' on every clear
        # event; defensive check in case other event-shaped rows ever
        # land with sender='plan_missing'.
        subject_raw = row.get("subject", "") if isinstance(row, dict) else ""
        if not isinstance(subject_raw, str):
            continue
        # Strip the optional ``[Audit]`` / ``[Event]`` title-contract
        # prefix the store applies to ``subject`` so we still match
        # rows whose subject was rewritten.
        subject_text = subject_raw
        if subject_text.startswith("["):
            close = subject_text.find("] ")
            if close > 0:
                subject_text = subject_text[close + 2:]
        if subject_text != "alert.cleared":
            continue
        scope = row.get("scope", "") if isinstance(row, dict) else ""
        if not isinstance(scope, str) or not scope:
            continue
        if not scope.startswith("plan_gate-"):
            continue
        cleared_at_raw = (
            row.get("created_at") if isinstance(row, dict) else None
        )
        cleared_at = _coerce_clear_timestamp(cleared_at_raw)
        if cleared_at is None:
            continue
        if cleared_at < cutoff:
            continue
        clears.append(_PlanMissingClear(scope=scope, cleared_at=cleared_at))
    return clears


def _coerce_clear_timestamp(value: Any) -> datetime | None:
    """Coerce a messages-table timestamp into tz-aware ``datetime``."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return None


def _self_heal_legacy_db_shadow(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
) -> dict[str, int]:
    """Drain one project's legacy per-project DB into the canonical workspace DB.

    #1519 self-healing action. Calls
    :func:`pollypm.storage.legacy_per_project_db.migrate_one` which is
    idempotent: rows already present in the workspace DB are skipped,
    source is archived to ``state.db.legacy-1004`` only after every row
    was either copied or matched. A re-run on an already-migrated
    workspace is a no-op (the source file is renamed away on success
    so the next probe finds no shadow).

    Returns ``{"migrated": 1, "failed": 0}`` on success or
    ``{"migrated": 0, "failed": 1}`` on any failure. Best-effort: a
    failure leaves the source DB in place so the next cadence tick can
    retry.
    """
    counters = {"migrated": 0, "failed": 0}
    if project_path is None:
        counters["failed"] = 1
        return counters
    try:
        from pollypm.storage.legacy_per_project_db import migrate_one
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: legacy migrate import failed", exc_info=True,
        )
        counters["failed"] = 1
        return counters
    canonical_db_raw = (finding.metadata or {}).get("canonical_db")
    if not canonical_db_raw:
        counters["failed"] = 1
        return counters
    try:
        report = migrate_one(
            project_key=project_key,
            project_path=Path(project_path),
            workspace_db=Path(canonical_db_raw),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "audit.watchdog: legacy_db_shadow migrate raised for %s",
            project_key, exc_info=True,
        )
        counters["failed"] = 1
        return counters
    if report.errors:
        logger.warning(
            "audit.watchdog: legacy_db_shadow migrate had errors for %s: %s",
            project_key, report.errors,
        )
        counters["failed"] = 1
        return counters
    # ``migrate_one`` returns ``succeeded=False`` when it skipped — the
    # source DB is missing or IS the workspace DB. Both are "no-op
    # success" for our purposes: a re-run on an already-healed project
    # (source archived to ``.legacy-1004`` on the previous tick) hits
    # this branch and should not be counted as a failure.
    logger.info(
        "audit.watchdog: legacy_db_shadow drained %s → canonical "
        "(copied=%s skipped=%s archived_to=%s skip_reason=%s)",
        project_key,
        {k: v for k, v in report.rows_copied.items() if v},
        {k: v for k, v in report.rows_skipped.items() if v},
        report.archived_to,
        report.skipped_reason,
    )
    counters["migrated"] = 1
    return counters


def _gather_storage_windows(storage_closet_name: str | None) -> list[str]:
    """Best-effort list of window names in the storage-closet session.

    Returns an empty list when tmux is unreachable — the
    ``role_session_missing`` rule then conservatively no-ops rather
    than firing false-positives during a tmux outage.
    """
    if not storage_closet_name:
        return []
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        if not tmux.has_session(storage_closet_name):
            return []
        windows = tmux.list_windows(storage_closet_name)
        return [getattr(w, "name", "") or "" for w in windows]
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: list_windows failed for %s",
            storage_closet_name, exc_info=True,
        )
        return []


def _architect_window_target(
    storage_closet_name: str | None, project_key: str,
) -> str | None:
    """Return ``<closet>:architect-<project>`` if the closet exists.

    The dispatch path send-keys's the brief to that window. Returning
    ``None`` means we can't dispatch right now (no closet, no architect
    window) — the cadence handler treats that as a soft-fail so the
    audit emit + alert still fire.
    """
    if not storage_closet_name or not project_key:
        return None
    return f"{storage_closet_name}:architect-{project_key}"


def _send_brief_to_architect(
    target: str, brief: str,
) -> bool:
    """Best-effort tmux send-keys of the brief to the architect window.

    Returns True on success. The brief is sent as a single text blob
    followed by Enter so the architect agent (Codex/Claude) actually
    processes the turn — agents don't act on their input buffer until
    Enter lands.

    #1420: previously called ``send_keys(..., press_enter=False)`` on
    the rationale that the architect should "review before submitting"
    (mirroring ``_perform_pm_dispatch``'s human-gated semantics). But
    ``_perform_pm_dispatch`` is for *user-initiated* cockpit dispatches
    where a human finalizes the send; here the receiver is an agent,
    so the brief sat in the prompt indefinitely until a human pressed
    Enter — undermining auto-unstick's "no user intervention" framing.

    The Enter is delivered by ``TmuxClient.send_keys`` after a 500ms
    settle delay on the paste-buffer path, mirroring the supervisor
    primer (``cockpit_rail.py`` ``_maybe_prime_*``) and chat dispatch
    convention (#1403, #1404, #1411).
    """
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        # press_enter=True so the agent submits the turn autonomously.
        # See module-level docstring + #1420 for the rationale.
        tmux.send_keys(target, brief, press_enter=True)
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: send_keys(%s) failed", target, exc_info=True,
        )
        return False


def _gather_reviewer_evidence(
    *,
    project_key: str,
    project_path: Path | None,
    subject: str,
    msg_store: Any,
    limit_executions: int = 2,
    limit_messages: int = 5,
) -> list[str]:
    """Collect reviewer execution rows + recent inbox messages for ``subject``.

    Returns a list of one-line strings safe to embed verbatim in the
    architect brief. Best-effort — any failure (DB unreachable, message
    store missing) returns an empty list and the brief just says "no
    additional reviewer evidence available". The caller treats the
    evidence as advisory only.

    The execution rows are filtered to the reviewer node (``code_review``
    or any node whose name contains ``review``) so the architect sees
    the rejecting verdict, not the worker's commits. Inbox messages are
    filtered to those that mention the subject so we don't dump the
    whole project queue into the brief.
    """
    evidence: list[str] = []

    # 1. Recent reviewer execution rows from the work-service DB.
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            try:
                task = svc.get(subject)
            except Exception:  # noqa: BLE001
                task = None
            if task is not None:
                # ``task.executions`` is the in-memory join the work
                # service hydrates on read (see SQLiteWorkService._load_executions).
                executions = list(getattr(task, "executions", None) or [])
                # Filter to reviewer-side rows; sort newest first; cap.
                review_rows = [
                    ex for ex in executions
                    if "review" in (getattr(ex, "node_id", "") or "").lower()
                ]
                review_rows.sort(
                    key=lambda ex: getattr(ex, "completed_at", None)
                    or getattr(ex, "started_at", None)
                    or "",
                    reverse=True,
                )
                for ex in review_rows[:limit_executions]:
                    decision = getattr(ex, "decision", None)
                    decision_value = getattr(decision, "value", decision) or "?"
                    reason = getattr(ex, "decision_reason", None) or ""
                    node = getattr(ex, "node_id", "") or "?"
                    completed = getattr(ex, "completed_at", None)
                    completed_s = (
                        completed.isoformat() if completed is not None else "?"
                    )
                    line = (
                        f"reviewer exec [{node} @ {completed_s}] "
                        f"decision={decision_value}"
                    )
                    if reason:
                        # Truncate aggressively — the brief is
                        # already structured.
                        clipped = reason.strip().splitlines()[0][:240]
                        line = f"{line} reason: {clipped}"
                    evidence.append(line)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: reviewer-execution fetch failed for %s",
            subject, exc_info=True,
        )

    # 2. Inbox messages that reference the task subject. We use the
    # generic ``query_messages`` surface (post-#349) and filter
    # client-side because every persona persists messages here. The
    # filter is deliberately permissive — anything mentioning the
    # canonical ``project/N`` string is considered relevant.
    try:
        if msg_store is not None and hasattr(msg_store, "query_messages"):
            scopes = [project_key] if project_key else []
            if "inbox" not in scopes:
                # Pre-#1425 reviewer notifies could land in the global
                # inbox scope even when the subject was project-specific.
                # Include that legacy scope so forensic briefs see the
                # actual reviewer finding instead of only the transition
                # reason that parked the task.
                scopes.append("inbox")
            rows: list[dict[str, Any]] = []
            seen_message_ids: set[Any] = set()
            for scope in scopes:
                for row in msg_store.query_messages(scope=scope) or []:
                    message_id = row.get("id")
                    if message_id is not None:
                        if message_id in seen_message_ids:
                            continue
                        seen_message_ids.add(message_id)
                    rows.append(row)
            matched: list[dict[str, Any]] = []
            for row in rows:
                body = (
                    str(row.get("body") or "")
                    + " "
                    + str(row.get("title") or "")
                    + " "
                    + str(row.get("subject") or "")
                )
                if subject in body or row.get("subject") == subject:
                    matched.append(row)
            # Sort by created_at desc when available, then cap.
            matched.sort(
                key=lambda r: r.get("created_at") or r.get("ts") or "",
                reverse=True,
            )
            for row in matched[:limit_messages]:
                role = (
                    row.get("requester")
                    or row.get("actor")
                    or row.get("from")
                    or "?"
                )
                title = row.get("title") or row.get("subject") or "(no title)"
                body = (row.get("body") or "").strip().splitlines()
                first_line = body[0][:240] if body else ""
                line = f"inbox msg from {role}: {title}"
                if first_line:
                    line = f"{line} — {first_line}"
                evidence.append(line)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: reviewer-message fetch failed for %s",
            subject, exc_info=True,
        )

    return evidence


def _enrich_finding_metadata(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    msg_store: Any,
) -> Finding:
    """Return a copy of ``finding`` with reviewer evidence folded in.

    Only :data:`RULE_TASK_ON_HOLD_STALE` is enriched today — the brief
    generator surfaces ``metadata['reviewer_evidence']`` for that rule.
    Other rules are returned unchanged. We rebuild a Finding rather than
    mutating because :class:`Finding` is frozen.
    """
    if finding.rule != RULE_TASK_ON_HOLD_STALE:
        return finding
    evidence = _gather_reviewer_evidence(
        project_key=project_key,
        project_path=project_path,
        subject=finding.subject,
        msg_store=msg_store,
    )
    if not evidence:
        return finding
    enriched_meta = {**(finding.metadata or {}), "reviewer_evidence": evidence}
    return Finding(
        rule=finding.rule,
        project=finding.project,
        subject=finding.subject,
        severity=finding.severity,
        message=finding.message,
        recommendation=finding.recommendation,
        metadata=enriched_meta,
    )


def _maybe_dispatch_to_architect(
    finding: Finding,
    *,
    project_path: Path | None,
    storage_closet_name: str | None,
    now: datetime,
) -> str:
    """Apply throttle + emit + send brief. Returns a status code.

    Status codes (used by the per-project counters):
    * ``"skipped"`` — rule isn't dispatchable.
    * ``"throttled"`` — already dispatched within the throttle window.
    * ``"dispatched"`` — brief sent successfully.
    * ``"send_failed"`` — emit ran, send-keys failed.
    """
    if finding.rule not in _DISPATCHABLE_RULES:
        return "skipped"
    if was_recently_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        now=now,
        project_path=project_path,
        throttle_seconds=ESCALATION_THROTTLE_SECONDS,
    ):
        return "throttled"
    brief = format_unstick_brief(finding)
    # Emit the dispatch event BEFORE the send so the throttle window
    # is engaged even if the send fails — otherwise a tmux outage would
    # cause every cadence tick to re-emit and re-attempt.
    emit_escalation_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        brief=brief,
        project_path=project_path,
    )
    target = _architect_window_target(storage_closet_name, finding.project)
    if target is None:
        return "send_failed"
    if not _send_brief_to_architect(target, brief):
        return "send_failed"
    return "dispatched"


def _self_heal_duplicate_advisor_tasks(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
) -> dict[str, int]:
    """Cancel every duplicate advisor task except the most-recently-created.

    #1510 self-healing action. The detector already chose which task to
    keep (``finding.metadata['keep_task_id']``) and which to cancel
    (``finding.metadata['duplicate_ids']``). We just iterate the list
    and call ``WorkService.cancel`` for each — using the work-service
    API rather than raw SQL so cancellation observers (audit emit,
    sync, alert teardown) all run.

    Returns ``{"cancelled": N, "failed": M}`` for the per-project
    counters. Best-effort: a per-task failure is logged and we keep
    going so a single bad row doesn't strand the rest.
    """
    counters = {"cancelled": 0, "failed": 0}
    duplicate_ids: list[str] = list(
        finding.metadata.get("duplicate_ids") or []
    )
    if not duplicate_ids:
        return counters
    reason = f"duplicate advisor_review (auto-cleanup #1510)"
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            for task_id in duplicate_ids:
                try:
                    svc.cancel(task_id, "audit_watchdog", reason)
                    counters["cancelled"] += 1
                except Exception:  # noqa: BLE001
                    counters["failed"] += 1
                    logger.warning(
                        "audit.watchdog: duplicate-advisor cleanup failed "
                        "for %s",
                        task_id, exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        # If we can't even open the work service, mark every duplicate
        # as failed so the totals reflect the wedge accurately.
        counters["failed"] += len(duplicate_ids) - counters["cancelled"]
        logger.warning(
            "audit.watchdog: duplicate-advisor cleanup could not open "
            "work service for %s", project_key, exc_info=True,
        )
    return counters


# ---------------------------------------------------------------------------
# Tier-1 healers (#1546)
# ---------------------------------------------------------------------------


def _self_heal_plan_review_missing(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    **_: Any,
) -> dict[str, int]:
    """Backfill the missing plan_review row for a plan_review_missing finding.

    Wraps :func:`_backfill_plan_review_for_finding` to return the
    counter shape every tier-1 healer emits. Idempotent because the
    underlying ``emit_plan_review_for_task`` helper re-checks the
    messages table before writing.
    """
    backfilled = _backfill_plan_review_for_finding(
        finding,
        project_key=project_key,
        project_path=project_path,
    )
    if backfilled is not None:
        return {"plan_review_backfilled": 1}
    return {"plan_review_backfill_failed": 1}


def _self_heal_legacy_db_shadow_counter(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    **_: Any,
) -> dict[str, int]:
    """Tier-1 registry adapter — translates the migrate counters to a
    common counter shape so the dispatcher can sum them generically.
    """
    raw = _self_heal_legacy_db_shadow(
        finding,
        project_key=project_key,
        project_path=project_path,
    )
    return {
        "legacy_db_shadows_migrated": raw.get("migrated", 0),
        "legacy_db_shadows_failed": raw.get("failed", 0),
    }


def _self_heal_duplicate_advisor_tasks_counter(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    **_: Any,
) -> dict[str, int]:
    """Tier-1 registry adapter — same shape as legacy-db-shadow."""
    raw = _self_heal_duplicate_advisor_tasks(
        finding,
        project_key=project_key,
        project_path=project_path,
    )
    return {
        "advisor_duplicates_cancelled": raw.get("cancelled", 0),
        "advisor_duplicates_failed": raw.get("failed", 0),
    }


def _self_heal_role_session_missing(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    config_path: Path | None = None,
    **_: Any,
) -> dict[str, int]:
    """#1546 — spawn the missing ``<role>-<project>`` lane.

    Re-uses the same plumbing ``pm worker-start`` does, but skips the
    deprecated ``--role worker`` rejection because the watchdog is
    auto-spawning a role lane (e.g. ``architect``, ``advisor``) on
    behalf of a queued / in-flight task. Best-effort: any failure is
    logged and we return a ``failed`` counter; a re-tick will retry.

    Idempotent — when the existing session lookup hits a match, we no-op
    and return ``{"worker_lane_spawned": 0}`` so the counter doesn't lie
    about how many lanes were actually created.
    """
    counters = {"worker_lane_spawned": 0, "worker_lane_failed": 0}
    role = (finding.metadata or {}).get("role") or ""
    if not role:
        counters["worker_lane_failed"] += 1
        return counters
    if role == "worker":
        # Per-task workers are provisioned automatically by ``pm task
        # claim``; spawning a long-running ``worker-<project>`` would
        # leak memory (see ``pm worker-start --role worker`` for the
        # rationale). Treat as a no-op so the cadence handler isn't
        # blamed for a lane it can't structurally repair.
        return counters
    try:
        from pollypm import cli as cli_mod
        from pollypm.config import DEFAULT_CONFIG_PATH

        # Only fall back to ``DEFAULT_CONFIG_PATH`` when the cadence
        # handler was invoked without an explicit config path AND the
        # default file actually exists. Tests that don't seed a config
        # file should not have the healer accidentally try to talk to
        # tmux on the developer's real workspace; ``config_path=None``
        # plus a missing default is treated as "we don't know enough
        # to safely spawn", and we increment failed without raising.
        cfg_path = config_path
        if cfg_path is None:
            if DEFAULT_CONFIG_PATH.exists():
                cfg_path = DEFAULT_CONFIG_PATH
            else:
                counters["worker_lane_failed"] += 1
                return counters
        supervisor = cli_mod._load_supervisor(cfg_path)
        existing = next(
            (
                session
                for session in supervisor.config.sessions.values()
                if session.role == role
                and session.project == project_key
                and session.enabled
            ),
            None,
        )
        if existing is None:
            session = cli_mod.create_worker_session(
                cfg_path,
                project_key=project_key,
                prompt=None,
                role=role,
                agent_profile=None,
            )
        else:
            session = existing
        cli_mod.launch_worker_session(cfg_path, session.name)
    except Exception:  # noqa: BLE001
        logger.warning(
            "audit.watchdog: role-lane spawn failed for %s/%s",
            project_key, role, exc_info=True,
        )
        counters["worker_lane_failed"] += 1
        return counters
    counters["worker_lane_spawned"] += 1
    # Best-effort forensic event so operators can grep the audit log
    # for spawn cadence.
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_WATCHDOG_WORKER_LANE_SPAWNED

        _audit_emit(
            event=EVENT_WATCHDOG_WORKER_LANE_SPAWNED,
            project=project_key,
            subject=finding.subject,
            actor="audit_watchdog",
            metadata={
                "role": role,
                "project": project_key,
                "task_subject": finding.subject,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: emit worker_lane_spawned failed",
            exc_info=True,
        )
    return counters


def _self_heal_state_db_missing(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    config_path: Path | None = None,
    **_: Any,
) -> dict[str, int]:
    """#1546 — re-create the canonical workspace state.db when it's missing.

    Post-workspace-root migration the canonical work DB lives at
    ``<workspace_root>/.pollypm/state.db`` (single file shared across
    all projects, row-level project isolation). Repair opens that DB
    through :func:`pollypm.work.create_work_service` — the schema
    replay in ``open_workspace_db`` + ``apply_workspace_pragmas`` runs
    every CREATE TABLE IF NOT EXISTS, so a missing file is created and
    populated with the canonical schema. Idempotent: opening an
    existing healthy DB is a no-op.

    Falls back to :func:`pollypm.projects.enable_tracked_project` only
    if the resolver couldn't locate a workspace DB (degenerate config) —
    that path re-emits the per-project issue tracker scaffold.
    """
    counters = {
        "state_db_repaired": 0,
        "state_db_repair_failed": 0,
    }

    # Prefer the workspace-root repair: open the canonical DB so
    # ``open_workspace_db`` materialises it on disk.
    canonical_db: Path | None = None
    finding_evidence = finding.evidence or {}
    raw_path = finding_evidence.get("canonical_db_path") or (finding.metadata or {}).get(
        "canonical_db_path"
    )
    if isinstance(raw_path, str) and raw_path:
        try:
            canonical_db = Path(raw_path)
        except TypeError:
            canonical_db = None
    if canonical_db is None:
        try:
            from pollypm.config import load_config

            cfg_path = config_path
            if cfg_path is None:
                from pollypm.config import DEFAULT_CONFIG_PATH

                if DEFAULT_CONFIG_PATH.exists():
                    cfg_path = DEFAULT_CONFIG_PATH
            cfg = load_config(cfg_path) if cfg_path else None
            from pollypm.work.db_resolver import resolve_work_db_path

            canonical_db = resolve_work_db_path(config=cfg)
        except Exception:  # noqa: BLE001
            canonical_db = None

    repaired_via_workspace = False
    if canonical_db is not None:
        try:
            canonical_db.parent.mkdir(parents=True, exist_ok=True)
            from pollypm.work import create_work_service

            svc = create_work_service(
                db_path=canonical_db,
                project_path=canonical_db.parent.parent,
            )
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
            repaired_via_workspace = canonical_db.exists()
        except Exception:  # noqa: BLE001
            logger.warning(
                "audit.watchdog: workspace state.db open failed at %s",
                canonical_db, exc_info=True,
            )

    if not repaired_via_workspace:
        # Fallback: re-run the per-project tracker scaffold so the
        # legacy code path still has an effect even when the workspace
        # resolver can't find a target.
        try:
            from pollypm.config import DEFAULT_CONFIG_PATH
            from pollypm.projects import enable_tracked_project

            cfg_path = config_path or DEFAULT_CONFIG_PATH
            enable_tracked_project(cfg_path, project_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "audit.watchdog: state_db_missing repair failed for %s",
                project_key, exc_info=True,
            )
            counters["state_db_repair_failed"] += 1
            return counters
    counters["state_db_repaired"] += 1
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_WATCHDOG_PROJECT_TRACKED_MODE_REPAIRED

        _audit_emit(
            event=EVENT_WATCHDOG_PROJECT_TRACKED_MODE_REPAIRED,
            project=project_key,
            subject=project_key,
            actor="audit_watchdog",
            metadata={
                "project_key": project_key,
                "project_path": str(project_path) if project_path else "",
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: emit project_tracked_mode_repaired failed",
            exc_info=True,
        )
    return counters


# Tier-1 healer registry — maps rule name to a ``(finding, **kwargs) ->
# dict[str, int]`` callable. Adding a tier-1 self-heal rule means
# writing the detector + registering the healer here. The dispatcher in
# ``_scan_one_project`` looks up the registry in O(1) and skips the
# old if/elif branches; behaviour for the existing 3 self-heal rules is
# byte-identical to the pre-#1546 implementation (the registry adapters
# wrap the original helpers and translate their counter shapes).
_TIER1_HEALERS: dict[str, Any] = {
    RULE_DUPLICATE_ADVISOR_TASKS: _self_heal_duplicate_advisor_tasks_counter,
    RULE_PLAN_REVIEW_MISSING: _self_heal_plan_review_missing,
    RULE_LEGACY_DB_SHADOW: _self_heal_legacy_db_shadow_counter,
    # #1546 — new tier-1 entries.
    RULE_ROLE_SESSION_MISSING: _self_heal_role_session_missing,
    RULE_STATE_DB_MISSING: _self_heal_state_db_missing,
}


# ---------------------------------------------------------------------------
# Tier-3 operator dispatch (#1546)
# ---------------------------------------------------------------------------


def _operator_dedup_key(rule: str, project: str, subject: str) -> str:
    """Stable dedup key for tier-3 inbox rows.

    Hash-folded so the inbox's ``--dedup-key`` collapses repeated
    operator dispatches to a single row whose ``count`` increments.
    Mirrors :func:`pollypm.audit.watchdog.watchdog_alert_session_name`
    in spirit but lives on the inbox side.
    """
    safe_subject = (subject or "").replace("/", "_").replace(" ", "_") or "_"
    return f"watchdog-operator:{rule}:{project}:{safe_subject}"


def _maybe_dispatch_to_operator(
    finding: Finding,
    *,
    project_path: Path | None,
    now: datetime,
    config_path: Path | None = None,
) -> str:
    """Tier-3 dispatch: route a finding to the operator's inbox.

    Symmetric to :func:`_maybe_dispatch_to_architect` but for the
    operator leg. Creates an inbox task via the same plumbing
    :func:`pm notify` uses (``store.enqueue_message`` +
    ``svc.create``), with ``roles.operator='user'`` and a structured
    evidence-and-question body produced by
    :func:`tier_handoff_prompt`. Uses the audit log itself as the
    throttle source-of-truth so a heartbeat-process restart doesn't
    reset the cooldown.

    Status codes (per-project counters):
    * ``"skipped"`` — rule isn't operator-dispatchable.
    * ``"throttled"`` — recent matching dispatch in the audit log.
    * ``"dispatched"`` — inbox row + audit event written.
    * ``"send_failed"`` — emit ran, inbox write raised.
    """
    if finding.rule not in _OPERATOR_DISPATCHABLE_RULES:
        return "skipped"
    if was_recently_operator_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        now=now,
        project_path=project_path,
        throttle_seconds=OPERATOR_DISPATCH_THROTTLE_SECONDS,
    ):
        return "throttled"

    # Build the body via the structured-evidence helper. We hand the
    # finding's evidence dict and a single decision-shaped question;
    # the helper refuses any solution-menu shape.
    question = (
        f"Project {finding.project} is wedged in a way the architect "
        f"can't unstick. What structural change do you want to apply?"
    )
    try:
        body = tier_handoff_prompt(finding.evidence or {}, question)
    except Exception:  # noqa: BLE001
        logger.warning(
            "audit.watchdog: tier_handoff_prompt raised for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        # Fall back to the finding message so we don't lose the dispatch.
        body = finding.message or finding.rule

    dedup_key = _operator_dedup_key(
        finding.rule, finding.project, finding.subject,
    )
    subject_text = (
        finding.message
        or f"watchdog: {finding.rule} on {finding.project}/{finding.subject}"
    )[:200]

    # #1546 — try the inbox write first and only emit the throttle /
    # success audit on the success path. If we throttled on a failure
    # the next tick would see the audit row, refuse to retry, and we'd
    # ship a heartbeat that *thinks* it dispatched but actually wrote
    # no inbox row. On failure we emit a distinct
    # ``tier3_dispatch_failed`` event so forensic reads can see the
    # attempt without poisoning the throttle window.
    inbox_task_id: str | None = None
    try:
        inbox_task_id = _create_operator_inbox_task(
            project_key=finding.project,
            project_path=project_path,
            subject=subject_text,
            body=body,
            dedup_key=dedup_key,
            config_path=config_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit.watchdog: operator inbox write failed for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_WATCHDOG_TIER3_DISPATCH_FAILED

            _audit_emit(
                event=EVENT_WATCHDOG_TIER3_DISPATCH_FAILED,
                project=finding.project,
                subject=finding.subject,
                actor="audit_watchdog",
                metadata={
                    "finding_type": finding.rule,
                    "dedup_key": dedup_key,
                    "error": type(exc).__name__,
                },
                project_path=project_path,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: tier3_dispatch_failed emit raised",
                exc_info=True,
            )
        return "send_failed"
    if inbox_task_id is None:
        try:
            from pollypm.audit import emit as _audit_emit
            from pollypm.audit.log import EVENT_WATCHDOG_TIER3_DISPATCH_FAILED

            _audit_emit(
                event=EVENT_WATCHDOG_TIER3_DISPATCH_FAILED,
                project=finding.project,
                subject=finding.subject,
                actor="audit_watchdog",
                metadata={
                    "finding_type": finding.rule,
                    "dedup_key": dedup_key,
                    "error": "inbox_task_id_none",
                },
                project_path=project_path,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: tier3_dispatch_failed emit raised",
                exc_info=True,
            )
        return "send_failed"
    # Inbox write succeeded — emit the canonical operator_dispatched
    # row that engages the throttle window for the next tick.
    emit_operator_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        inbox_task_id=inbox_task_id,
        dedup_key=dedup_key,
        project_path=project_path,
    )
    return "dispatched"


def _create_operator_inbox_task(
    *,
    project_key: str,
    project_path: Path | None,
    subject: str,
    body: str,
    dedup_key: str,
    config_path: Path | None = None,
) -> str | None:
    """Materialise the operator inbox task. Returns ``task_id`` on success.

    Mirrors the relevant slice of :func:`pollypm.cli_features.session_runtime.notify`
    (the immediate-priority branch) — opens the workspace SQLAlchemy
    store, enqueues a notify message with the dedup_key, then creates
    the work-service task with ``roles.operator='user'`` and the
    ``notify`` label. Any failure raises and the caller logs it.

    ``config_path`` (#1546 fix) — threads the active cadence config so
    alternate-config heartbeat runs route the inbox task to the right
    workspace DB. Pre-fix this called ``_resolve_db_path`` without any
    config and a heartbeat run with ``--config /path/to/alt.toml``
    would write inbox rows to whichever DB ``DEFAULT_CONFIG_PATH``
    happened to name.
    """
    from pollypm.inbox_dedup import (
        bump_dedup_message,
        find_open_dedup_message,
        initial_dedup_payload,
    )
    from pollypm.store import SQLAlchemyStore
    from pollypm.work import create_work_service
    from pollypm.work.db_resolver import resolve_work_db_path

    resolved_config: Any | None = None
    if config_path is not None:
        try:
            from pollypm.config import load_config

            resolved_config = load_config(config_path)
        except Exception:  # noqa: BLE001
            resolved_config = None
    db_path = resolve_work_db_path(
        ".pollypm/state.db",
        project=project_key,
        config=resolved_config,
    )
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    payload = {
        "actor": "audit_watchdog",
        "project": project_key,
        "milestone_key": None,
        "requester": "user",
    }
    try:
        existing = find_open_dedup_message(
            store, dedup_key, recipient="user",
        ) if dedup_key else None
        if existing is not None:
            message_id = bump_dedup_message(
                store,
                existing,
                subject=subject,
                body=body,
                payload={**payload, "dedup_key": dedup_key},
                labels=["notify", "watchdog"],
                tier="immediate",
            )
        else:
            seeded_payload = (
                initial_dedup_payload(payload, dedup_key)
                if dedup_key else payload
            )
            message_id = store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="audit_watchdog",
                subject=subject,
                body=body,
                scope=project_key,
                labels=["notify", "watchdog"],
                payload=seeded_payload,
                state="closed",
            )
    finally:
        store.close()

    svc = create_work_service(
        db_path=db_path,
        project_path=db_path.parent.parent,
    )
    try:
        task_labels = [
            "notify",
            "watchdog",
            f"notify_message:{message_id}",
        ]
        task = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project_key,
            flow_template="chat",
            roles={
                "requester": "user",
                "operator": "user",
            },
            priority="high",
            created_by="audit_watchdog",
            labels=task_labels,
        )
        inbox_task_id = task.task_id
        store2 = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store2.update_message(
                message_id,
                payload={**payload, "task_id": inbox_task_id},
            )
        finally:
            store2.close()
        return inbox_task_id
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# Tier-4 operator dispatch (#1553 broader-authority leg)
# ---------------------------------------------------------------------------


def _resolve_workspace_state_db_path() -> Path | None:
    """Return the canonical workspace state.db path or ``None`` on failure.

    The tier-4 tracker needs the workspace-wide DB (not the per-project
    one) because promotion accounting spans projects. We resolve it via
    the same ``_resolve_db_path`` helper the tier-3 inbox writer uses,
    asking for ``project=None`` to land on the workspace DB.
    """
    try:
        from pollypm.work.cli import _resolve_db_path
        return _resolve_db_path(".pollypm/state.db", project=None)
    except Exception:  # noqa: BLE001
        logger.debug(
            "tier4: workspace state.db resolve failed", exc_info=True,
        )
        return None


def _build_tier4_tracker() -> Any | None:
    """Construct a :class:`Tier4PromotionTracker` against the workspace DB."""
    try:
        from pollypm.audit.tier4 import Tier4PromotionTracker
    except ImportError:  # pragma: no cover — defensive
        return None
    db_path = _resolve_workspace_state_db_path()
    if db_path is None:
        return None
    return Tier4PromotionTracker(db_path)


def _send_tier4_global_action_push(
    *,
    title: str,
    body: str,
) -> bool:
    """Fire a desktop notification for a tier-4 system-scoped action.

    Best-effort. Returns True iff at least one notifier accepted the
    payload. Used by :func:`emit_tier4_global_action` so every
    ``audit.tier4_global_action`` row is paired with a louder signal
    to Sam (per the spec). Failure is silent — the audit row is the
    durable signal; the desktop ping is the louder one.
    """
    try:
        from pollypm.error_notifications import (
            LinuxNotifySendNotifier,
            MacOsDesktopNotifier,
        )
    except ImportError:
        return False
    delivered = False
    for notifier in (MacOsDesktopNotifier(), LinuxNotifySendNotifier()):
        try:
            if not notifier.is_available():
                continue
            notifier.notify(title=title, body=body[:280])
            delivered = True
        except Exception:  # noqa: BLE001
            continue
    return delivered


def _create_operator_tier4_inbox_task(
    *,
    project_key: str,
    project_path: Path | None,
    subject: str,
    body: str,
    dedup_key: str,
) -> str | None:
    """Materialise the tier-4 operator inbox task.

    Mirrors :func:`_create_operator_inbox_task` exactly except for the
    label set: tier-4 cards carry ``"tier4"`` so the cockpit / Polly's
    inbox query can distinguish them. Reuses the same enqueue + create
    plumbing so the inbox surface stays unified — only the prompt + the
    label set differ.
    """
    from pollypm.inbox_dedup import (
        bump_dedup_message,
        find_open_dedup_message,
        initial_dedup_payload,
    )
    from pollypm.store import SQLAlchemyStore
    from pollypm.work import create_work_service
    from pollypm.work.cli import _resolve_db_path

    db_path = _resolve_db_path(".pollypm/state.db", project=project_key)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    payload = {
        "actor": "audit_watchdog",
        "project": project_key,
        "milestone_key": None,
        "requester": "user",
        "tier": "4",
    }
    try:
        existing = find_open_dedup_message(
            store, dedup_key, recipient="user",
        ) if dedup_key else None
        if existing is not None:
            message_id = bump_dedup_message(
                store,
                existing,
                subject=subject,
                body=body,
                payload={**payload, "dedup_key": dedup_key},
                labels=["notify", "watchdog", "tier4"],
                tier="immediate",
            )
        else:
            seeded_payload = (
                initial_dedup_payload(payload, dedup_key)
                if dedup_key else payload
            )
            message_id = store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="audit_watchdog",
                subject=subject,
                body=body,
                scope=project_key,
                labels=["notify", "watchdog", "tier4"],
                payload=seeded_payload,
                state="closed",
            )
    finally:
        store.close()

    svc = create_work_service(
        db_path=db_path,
        project_path=db_path.parent.parent,
    )
    try:
        task_labels = [
            "notify",
            "watchdog",
            "tier4",
            f"notify_message:{message_id}",
        ]
        task = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project_key,
            flow_template="chat",
            roles={
                "requester": "user",
                "operator": "user",
            },
            priority="high",
            created_by="audit_watchdog",
            labels=task_labels,
        )
        inbox_task_id = task.task_id
        store2 = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store2.update_message(
                message_id,
                payload={**payload, "task_id": inbox_task_id},
            )
        finally:
            store2.close()
        return inbox_task_id
    finally:
        svc.close()


def _build_tier4_body(
    finding: Finding,
    *,
    root_cause_hash_value: str,
    promotion_path: str,
    tracker: Any,
    justification: str = "",
) -> str:
    """Compose the tier-4 dispatch body.

    Layout:

        TIER 4 BROADER AUTHORITY DISPATCH
        <runtime block: hash, promotion path, budget, threshold>
        <authority section from tier4_authority.md>

        --- Finding ---
        <tier_handoff_prompt(evidence, question)>

        <self-promote justification, if any>

    The handoff prompt is structured-evidence + a question (the
    helper refuses solution-menu shapes). The authority section is
    static markdown spliced from disk. The runtime block lets Polly
    quote the live constants back without re-reading the spec.
    """
    from pollypm.agents.tier4_authority import render_tier4_authority_section

    auth = render_tier4_authority_section(
        root_cause_hash_value=root_cause_hash_value,
        promotion_path=promotion_path,
        budget_seconds=getattr(tracker, "tier4_budget_seconds", 7200),
        auto_promote_threshold=getattr(tracker, "auto_promote_threshold", 3),
        auto_promote_window_seconds=getattr(
            tracker, "auto_promote_window_seconds", 86400,
        ),
    )
    question = (
        f"Project {finding.project} is in tier-4 broader-authority mode "
        f"(promotion_path={promotion_path}, root_cause_hash="
        f"{root_cause_hash_value}). What action do you take, within the "
        f"integrity rules above?"
    )
    try:
        handoff = tier_handoff_prompt(finding.evidence or {}, question)
    except Exception:  # noqa: BLE001
        logger.warning(
            "tier4: tier_handoff_prompt raised for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        handoff = finding.message or finding.rule

    parts: list[str] = [
        "TIER 4 BROADER AUTHORITY DISPATCH",
        "",
        auth,
        "",
        "--- Finding ---",
        handoff,
    ]
    if justification:
        parts.append("")
        parts.append("--- Self-promote justification ---")
        parts.append(justification.strip())
    return "\n".join(parts)


def _emit_tier4_promoted(
    finding: Finding,
    *,
    root_cause_hash_value: str,
    promotion_path: str,
    project_path: Path | None,
    justification: str = "",
) -> None:
    """Emit ``audit.tier4_promoted``. Best-effort; never raises."""
    from pollypm.audit.log import EVENT_TIER4_PROMOTED
    from pollypm.audit.log import emit as _audit_emit

    try:
        _audit_emit(
            event=EVENT_TIER4_PROMOTED,
            project=finding.project,
            subject=finding.subject,
            actor="audit_watchdog",
            status="warn",
            metadata={
                "finding_type": finding.rule,
                "subject": finding.subject,
                "root_cause_hash": root_cause_hash_value,
                "promotion_path": promotion_path,
                "justification": justification,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("tier4: emit_tier4_promoted failed", exc_info=True)


def _emit_tier4_dispatched(
    finding: Finding,
    *,
    root_cause_hash_value: str,
    promotion_path: str,
    inbox_task_id: str | None,
    dedup_key: str,
    project_path: Path | None,
) -> None:
    """Emit ``watchdog.operator_tier4_dispatched``."""
    from pollypm.audit.log import EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED
    from pollypm.audit.log import emit as _audit_emit

    try:
        _audit_emit(
            event=EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED,
            project=finding.project,
            subject=finding.subject,
            actor="audit_watchdog",
            status="warn",
            metadata={
                "finding_type": finding.rule,
                "subject": finding.subject,
                "root_cause_hash": root_cause_hash_value,
                "promotion_path": promotion_path,
                "inbox_task_id": inbox_task_id or "",
                "dedup_key": dedup_key,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("tier4: emit_tier4_dispatched failed", exc_info=True)


def _dispatch_to_operator_tier4(
    finding: Finding,
    *,
    project_path: Path | None,
    now: datetime,
    promotion_path: str,
    justification: str = "",
    tracker: Any | None = None,
) -> str:
    """Tier-4 dispatch: route a finding to broader-authority Polly.

    Symmetric to :func:`_maybe_dispatch_to_operator` but loads the
    tier-4 authority section into the body and tags the inbox row
    with ``tier4`` so Polly's inbox query can distinguish it.

    Status codes (per-project counters):
    * ``"throttled"`` — tier-4 already active for this hash; let the
      existing run finish or budget-exhaust before re-dispatching.
    * ``"dispatched"`` — promotion + inbox + audit all wrote.
    * ``"send_failed"`` — promotion recorded, inbox write raised.

    The caller (auto-promote branch in ``_scan_one_project`` OR the
    self-promote CLI) decides whether to call this; it does not gate
    on ``_OPERATOR_DISPATCHABLE_RULES`` because tier-4 dispatch is
    promotion-driven, not rule-driven.
    """
    from pollypm.audit.tier4 import root_cause_hash

    if tracker is None:
        tracker = _build_tier4_tracker()
    if tracker is None:
        # Tracker unavailable means we can't enforce promotion / budget,
        # which means we can't safely dispatch at tier-4. Fall back to
        # tier-3 — the caller can detect this and re-route.
        return "send_failed"

    rch = root_cause_hash(finding)
    existing = tracker.get(rch)
    # Don't re-promote into an active tier-4 run. The cadence handler's
    # budget-enforcement sweep will demote the existing run when its
    # budget exhausts; until then, dispatch is throttled to avoid
    # multi-promote interleaving.
    if existing is not None and existing.tier4_active:
        return "throttled"

    # Build the inbox payload + write it FIRST. Recording the promotion
    # before the inbox write would leave ``tier4_active=1`` when the
    # write fails, throttling all future tier-4 dispatches for this
    # root_cause_hash until the budget expired. We only flip the
    # tracker into the active state once the inbox row is committed.
    body = _build_tier4_body(
        finding,
        root_cause_hash_value=rch,
        promotion_path=promotion_path,
        tracker=tracker,
        justification=justification,
    )
    dedup_key = f"watchdog-operator-tier4:{finding.rule}:{finding.project}:{rch}"
    subject_text = (
        finding.message
        or f"watchdog tier4: {finding.rule} on {finding.project}"
    )[:200]

    inbox_task_id: str | None = None
    try:
        inbox_task_id = _create_operator_tier4_inbox_task(
            project_key=finding.project,
            project_path=project_path,
            subject=subject_text,
            body=body,
            dedup_key=dedup_key,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "tier4: inbox write failed for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        return "send_failed"
    if inbox_task_id is None:
        return "send_failed"

    # Inbox write committed — now record the promotion + audit it.
    tracker.record_promotion(
        finding,
        now=now,
        promotion_path=promotion_path,
        justification=justification,
    )
    _emit_tier4_promoted(
        finding,
        root_cause_hash_value=rch,
        promotion_path=promotion_path,
        project_path=project_path,
        justification=justification,
    )
    _emit_tier4_dispatched(
        finding,
        root_cause_hash_value=rch,
        promotion_path=promotion_path,
        inbox_task_id=inbox_task_id,
        dedup_key=dedup_key,
        project_path=project_path,
    )
    return "dispatched"


def emit_tier4_action(
    *,
    project: str,
    action: str,
    root_cause_hash_value: str,
    actor: str = "polly",
    metadata: dict[str, Any] | None = None,
    project_path: Path | None = None,
) -> None:
    """Emit ``audit.tier4_action`` for a project-scoped tier-4 action.

    Public surface — Polly calls this (via a thin CLI wrapper or
    direct import in helpers) when she takes a project-scoped action
    under broadened authority. Best-effort; never raises.
    """
    from pollypm.audit.log import EVENT_TIER4_ACTION
    from pollypm.audit.log import emit as _audit_emit

    payload = dict(metadata or {})
    payload["action"] = action
    payload["root_cause_hash"] = root_cause_hash_value
    try:
        _audit_emit(
            event=EVENT_TIER4_ACTION,
            project=project,
            subject=action,
            actor=actor,
            status="warn",
            metadata=payload,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_tier4_action failed", exc_info=True)


def _emit_tier4_demoted(
    *,
    project: str,
    root_cause_hash_value: str,
    reason: str,
    project_path: Path | None = None,
) -> None:
    """Emit ``audit.tier4_demoted``. Best-effort."""
    from pollypm.audit.log import EVENT_TIER4_DEMOTED
    from pollypm.audit.log import emit as _audit_emit

    try:
        _audit_emit(
            event=EVENT_TIER4_DEMOTED,
            project=project,
            subject=root_cause_hash_value,
            actor="audit_watchdog",
            metadata={
                "root_cause_hash": root_cause_hash_value,
                "reason": reason,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_tier4_demoted failed", exc_info=True)


def _emit_tier4_budget_exhausted(
    *,
    project: str,
    root_cause_hash_value: str,
    elapsed_seconds: int,
    budget_seconds: int,
    urgent_handoff_subject: str,
    project_path: Path | None = None,
) -> None:
    """Emit ``audit.tier4_budget_exhausted``. Best-effort."""
    from pollypm.audit.log import EVENT_TIER4_BUDGET_EXHAUSTED
    from pollypm.audit.log import emit as _audit_emit

    try:
        _audit_emit(
            event=EVENT_TIER4_BUDGET_EXHAUSTED,
            project=project,
            subject=root_cause_hash_value,
            actor="audit_watchdog",
            status="error",
            metadata={
                "root_cause_hash": root_cause_hash_value,
                "elapsed_seconds": int(elapsed_seconds),
                "budget_seconds": int(budget_seconds),
                "urgent_handoff_subject": urgent_handoff_subject,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_tier4_budget_exhausted failed", exc_info=True)


def _route_tier4_to_terminal(
    *,
    state: Any,
    services: Any,
    now: datetime,
) -> bool:
    """Route a tier-4 row past its budget to the terminal path.

    Sets ``product_state=broken`` (idempotent) and creates an urgent
    inbox handoff via :func:`build_urgent_human_handoff`. Returns True
    iff the urgent handoff was successfully written; the caller still
    flips ``tier4_active=0`` regardless because the budget is up.

    Mock-able via the ``services`` argument: the StateStore + the inbox
    sink both come from there so tests can pass a stub.
    """
    from pollypm.audit.tier4 import TIER4_BUDGET_SECONDS
    from pollypm.audit.watchdog import build_urgent_human_handoff
    from pollypm.storage.product_state import set_product_state_broken

    elapsed = (
        int((now - state.tier4_entered_at).total_seconds())
        if state.tier4_entered_at else TIER4_BUDGET_SECONDS
    )
    forensics_path = (
        f"~/.pollypm/audit/{state.project or 'workspace'}.jsonl"
    )
    hypothesis = (
        f"Tier-4 broader-authority Polly worked on {state.project or 'workspace'}/"
        f"{state.rule} for {elapsed}s without resolving the finding. "
        f"The cascade is exhausted; Sam decides the next step."
    )
    handoff = build_urgent_human_handoff(
        tried=[
            f"Tier-3 Polly dispatched for rule={state.rule}",
            f"Auto-promoted to tier-4 (root_cause_hash={state.root_cause_hash})",
            "Tier-4 ran for the full wall-clock budget without clearing",
        ],
        failed=[
            "Tier-4 broader-authority budget exhausted before resolution",
        ],
        hypothesis=hypothesis,
        forensics_path=forensics_path,
        project=state.project,
        subject_hint=(
            f"Tier-4 budget exhausted on {state.project or 'workspace'}/{state.rule}"
        ),
    )
    # Set product_state=broken via the workspace state store. Best-effort.
    state_store = getattr(services, "state_store", None)
    try:
        if state_store is not None:
            set_product_state_broken(
                state_store,
                reason=(
                    f"Tier-4 cascade exhausted on {state.project or 'workspace'}/"
                    f"{state.rule}"
                ),
                forensics_path=forensics_path,
                set_by="audit_watchdog",
                extra={
                    "root_cause_hash": state.root_cause_hash,
                    "rule": state.rule,
                    "project": state.project,
                    "elapsed_seconds": elapsed,
                },
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "tier4: set_product_state_broken raised", exc_info=True,
        )

    # Drop the urgent inbox row via the same plumbing the tier-3 leg uses.
    msg_store = getattr(services, "msg_store", None)
    if msg_store is None:
        return False
    try:
        message_id = msg_store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="audit_watchdog",
            subject=handoff.subject,
            body=handoff.body,
            scope=state.project or "workspace",
            labels=list(handoff.labels) + ["tier4-terminal"],
            payload={
                "actor": "audit_watchdog",
                "project": state.project,
                "root_cause_hash": state.root_cause_hash,
                "tier": "terminal",
                "forensics_path": handoff.forensics_path,
            },
            state="closed",
        )
        return bool(message_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "tier4: urgent handoff enqueue raised", exc_info=True,
        )
        return False


def _sweep_tier4_budget_and_demotion(
    *,
    services: Any,
    now: datetime,
    seen_root_cause_hashes: set[str] | None = None,
) -> dict[str, int]:
    """Per-tick sweep — enforce budget AND demote on finding-resolved.

    For each row in ``tier4_promotion_state`` with ``tier4_active=1``:

    * If ``seen_root_cause_hashes`` is supplied (i.e. the cadence has
      finished its per-project finding scan this tick) and the row's
      ``root_cause_hash`` is NOT in the set, the underlying finding has
      resolved while tier-4 was active. Demote the row, emit
      ``audit.tier4_demoted`` with reason ``finding_resolved``. Without
      this, a fast tier-4 fix still hit the 2h budget → terminal/
      product-broken — defeating the recovery (#1554 HIGH-1).
    * If ``now - tier4_entered_at`` >= budget → fire the terminal path:
      emit ``audit.tier4_budget_exhausted``, route to product-broken +
      urgent inbox via :func:`_route_tier4_to_terminal`, then demote.
    * Otherwise leave the row alone — let the next tick check again.

    Resolved-finding demotion runs BEFORE the budget check so a row
    that resolved exactly at the budget boundary takes the demotion
    path (good outcome) rather than the terminal one. Pass
    ``seen_root_cause_hashes=None`` to skip resolved-finding detection
    (callers without finding-scan output, e.g. ad-hoc invocations).

    Returns counter increments to fold into the cadence totals.
    """
    counters: dict[str, int] = {
        "tier4_budget_exhausted": 0,
        "tier4_demoted_cleared": 0,
    }
    tracker = _build_tier4_tracker()
    if tracker is None:
        return counters
    try:
        active_rows = tracker.list_active()
    except Exception:  # noqa: BLE001
        logger.debug("tier4: list_active raised", exc_info=True)
        return counters
    for state in active_rows:
        try:
            # 1) Finding-resolved demotion. Only runs when the caller
            # supplied a seen-hash set — otherwise we can't safely
            # distinguish "finding gone" from "no scan happened yet".
            if (
                seen_root_cause_hashes is not None
                and state.root_cause_hash not in seen_root_cause_hashes
            ):
                cleared = tracker.clear(
                    state.root_cause_hash,
                    now=now,
                    reason="finding_resolved",
                )
                if cleared:
                    _emit_tier4_demoted(
                        project=state.project,
                        root_cause_hash_value=state.root_cause_hash,
                        reason="finding_resolved",
                    )
                    counters["tier4_demoted_cleared"] += 1
                continue

            # 2) Budget enforcement.
            if tracker.is_budget_exhausted(state.root_cause_hash, now=now):
                elapsed = (
                    int((now - state.tier4_entered_at).total_seconds())
                    if state.tier4_entered_at else 0
                )
                _emit_tier4_budget_exhausted(
                    project=state.project,
                    root_cause_hash_value=state.root_cause_hash,
                    elapsed_seconds=elapsed,
                    budget_seconds=tracker.tier4_budget_seconds,
                    urgent_handoff_subject=(
                        f"Tier-4 budget exhausted on {state.project}/{state.rule}"
                    ),
                )
                _route_tier4_to_terminal(
                    state=state, services=services, now=now,
                )
                tracker.clear(
                    state.root_cause_hash,
                    now=now,
                    reason="budget_exhausted",
                )
                counters["tier4_budget_exhausted"] += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "tier4: per-row sweep raised for %s",
                state.root_cause_hash, exc_info=True,
            )
    return counters


def clear_tier4_for_finding(
    finding: Finding,
    *,
    now: datetime,
    project_path: Path | None = None,
    reason: str = "finding_cleared",
) -> bool:
    """Public helper — clear tier-4 state when a finding terminally resolves.

    Used by callers that observe a finding go terminal-good (the
    project recovered). Idempotent: returns False if no row was active
    for the finding's root_cause_hash.
    """
    from pollypm.audit.tier4 import root_cause_hash

    tracker = _build_tier4_tracker()
    if tracker is None:
        return False
    rch = root_cause_hash(finding)
    state = tracker.get(rch)
    if state is None or not state.tier4_active:
        return False
    cleared = tracker.clear(rch, now=now, reason=reason)
    if cleared:
        _emit_tier4_demoted(
            project=finding.project,
            root_cause_hash_value=rch,
            reason=reason,
            project_path=project_path,
        )
    return cleared


def emit_tier4_global_action(
    *,
    action: str,
    root_cause_hash_value: str,
    actor: str = "polly",
    metadata: dict[str, Any] | None = None,
    project_path: Path | None = None,
    project: str = "",
    push_title: str | None = None,
    push_body: str | None = None,
) -> bool:
    """Emit ``audit.tier4_global_action`` AND fire a push notification.

    The "louder audit signature" the spec calls out: every
    system-scoped action under tier-4 authority pairs the audit row
    with a desktop notification so Sam knows the system was bounced.
    Returns True iff at least one notifier accepted the payload (for
    test-side assertions). Audit emit is best-effort and isolated
    from the push.
    """
    from pollypm.audit.log import EVENT_TIER4_GLOBAL_ACTION
    from pollypm.audit.log import emit as _audit_emit

    payload = dict(metadata or {})
    payload["action"] = action
    payload["root_cause_hash"] = root_cause_hash_value
    payload["scope"] = "system"
    try:
        _audit_emit(
            event=EVENT_TIER4_GLOBAL_ACTION,
            project=project or "",
            subject=action,
            actor=actor,
            status="warn",
            metadata=payload,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_tier4_global_action: emit failed", exc_info=True)

    title = push_title or f"PollyPM tier-4: {action}"
    body = push_body or (
        f"Tier-4 Polly invoked system-scoped action `{action}`. "
        f"root_cause_hash={root_cause_hash_value}."
    )
    return _send_tier4_global_action_push(title=title, body=body)


# ---------------------------------------------------------------------------
# State-DB-missing probe (#1546 tier-1)
# ---------------------------------------------------------------------------


def _gather_state_db_probes(
    *,
    services: Any,
    config: Any | None = None,
) -> list[_StateDbProbe]:
    """Probe each known project for canonical-state.db presence (#1546).

    Returns one descriptor per project. The pure
    :func:`_detect_state_db_missing` decides whether to fire; the
    self-heal action repairs by opening the workspace DB through the
    work service.

    Post-workspace-root migration (#339 / #1004): the canonical work
    DB is ``<workspace_root>/.pollypm/state.db`` — a single file shared
    across all known projects. We resolve it once per tick via
    :func:`pollypm.work.db_resolver.resolve_work_db_path` (the same
    source of truth ``pm task list`` and the cockpit use) so the probe
    can't drift from the resolver.
    """
    from pollypm.work.db_resolver import resolve_work_db_path

    probes: list[_StateDbProbe] = []
    known = getattr(services, "known_projects", None) or ()
    # Resolve the canonical workspace DB path once. ``resolve_work_db_path``
    # falls back to a cwd-relative default when no config is loadable —
    # we still want a valid path object for the probe so the detector
    # has something to report.
    services_config = getattr(services, "config", None)
    resolved_config = config if config is not None else services_config
    try:
        canonical_db = resolve_work_db_path(config=resolved_config)
    except Exception:  # noqa: BLE001
        canonical_db = None
    canonical_present = bool(canonical_db and canonical_db.exists())
    legacy_archives_present = False
    if canonical_db is not None:
        try:
            workspace_dir = canonical_db.parent
            if workspace_dir.exists():
                legacy_archives_present = any(
                    workspace_dir.glob("state.db.legacy-*")
                )
        except OSError:
            legacy_archives_present = False

    for project in known:
        project_key = getattr(project, "key", None) or getattr(
            project, "name", None,
        )
        project_path_raw = getattr(project, "path", None)
        if not project_key or project_path_raw is None:
            continue
        try:
            project_path = Path(project_path_raw)
        except TypeError:
            continue
        probes.append(_StateDbProbe(
            project_key=project_key,
            project_path=project_path,
            canonical_present=canonical_present,
            legacy_archives_present=legacy_archives_present,
            canonical_db_path=canonical_db,
        ))
    return probes


def _scan_one_project(
    *,
    project_key: str,
    project_path: Path | None,
    msg_store: Any,
    state_store: Any,
    now: datetime,
    config: WatchdogConfig,
    storage_closet_name: str | None = None,
    legacy_db_shadows: list[_LegacyDbShadow] | None = None,
    plan_missing_clears: list[_PlanMissingClear] | None = None,
    state_db_probes: list[_StateDbProbe] | None = None,
    config_path: Path | None = None,
) -> dict[str, int]:
    """Scan one project and route every finding. Returns counters."""
    counters: dict[str, int] = {
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
        "dispatches_sent": 0,
        "dispatches_throttled": 0,
        "dispatches_failed": 0,
        "advisor_duplicates_cancelled": 0,
        "advisor_duplicates_failed": 0,
        "plan_review_backfilled": 0,
        "plan_review_backfill_failed": 0,
        "legacy_db_shadows_migrated": 0,
        "legacy_db_shadows_failed": 0,
        "plan_missing_churn_detected": 0,
        # #1546 — new tier-1 healer counters.
        "worker_lane_spawned": 0,
        "worker_lane_failed": 0,
        "state_db_repaired": 0,
        "state_db_repair_failed": 0,
        # #1546 — tier-3 dispatcher counters.
        "operator_dispatches_sent": 0,
        "operator_dispatches_throttled": 0,
        "operator_dispatches_failed": 0,
        # #1553 — tier-4 dispatcher counters.
        "tier4_dispatches_sent": 0,
        "tier4_dispatches_throttled": 0,
        "tier4_dispatches_failed": 0,
        "tier4_demoted_cleared": 0,
        "tier4_budget_exhausted": 0,
    }
    open_tasks = _gather_open_tasks(project_key, project_path)
    storage_window_names = _gather_storage_windows(storage_closet_name)
    done_plan_tasks = _gather_done_plan_tasks(project_key, project_path)
    plan_review_present = (
        _make_plan_review_probe(project_key, project_path)
        if done_plan_tasks
        else None
    )
    # #1519 — filter the workspace-wide shadow list down to this
    # project's entry (if any) so the pure detector only sees what's
    # in scope for the current scan_project call.
    project_shadows: list[_LegacyDbShadow] = [
        s for s in (legacy_db_shadows or [])
        if s.project_key == project_key
    ]
    # #1524 — filter the workspace-wide clear list down to this
    # project's plan_gate-<project> scope so the pure detector only
    # sees what's in scope for the current scan_project call.
    expected_scope = f"plan_gate-{project_key}"
    project_clears: list[_PlanMissingClear] = [
        c for c in (plan_missing_clears or [])
        if c.scope == expected_scope
    ]
    # #1546 — filter the workspace-wide state-db probe list to this
    # project's row.
    project_state_probes: list[_StateDbProbe] = [
        p for p in (state_db_probes or [])
        if p.project_key == project_key
    ]
    try:
        findings = scan_project(
            project_key,
            project_path=project_path,
            now=now,
            config=config,
            open_tasks=open_tasks,
            storage_window_names=storage_window_names,
            done_plan_tasks=done_plan_tasks,
            plan_review_present=plan_review_present,
            legacy_db_shadows=project_shadows or None,
            plan_missing_clears=project_clears or None,
            state_db_probes=project_state_probes or None,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: scan_project(%s) failed",
            project_key, exc_info=True,
        )
        return counters

    # #1554 HIGH-1 — collect the root_cause_hash for every finding seen
    # this scan. The cadence-level sweep uses the union across projects
    # to detect tier-4 rows whose finding has resolved (hash absent →
    # demote, no terminal-path required). Stored on the counters dict
    # under a sentinel key the cadence handler pops out.
    from pollypm.audit.tier4 import root_cause_hash as _root_cause_hash

    seen_hashes: set[str] = set()
    for finding in findings:
        try:
            seen_hashes.add(_root_cause_hash(finding))
        except Exception:  # noqa: BLE001
            logger.debug(
                "tier4: root_cause_hash failed for %s/%s",
                finding.rule, finding.project, exc_info=True,
            )
    counters["_seen_root_cause_hashes"] = seen_hashes  # type: ignore[assignment]

    for finding in findings:
        counters["findings"] += 1
        emit_finding(finding)
        if _route_to_alert_sink(
            finding, msg_store=msg_store, state_store=state_store,
        ):
            counters["alerts_raised"] += 1
        else:
            counters["alert_failures"] += 1
            # Last-resort surface so a finding with no alert sink
            # still lands somewhere visible to operators.
            logger.warning(
                "audit.watchdog finding [%s] %s/%s: %s | %s",
                finding.rule, finding.project, finding.subject,
                finding.message, finding.recommendation,
            )

        # #1546 — tier-1 healer registry replaces the pre-#1546 if/elif
        # branches. Each healer returns ``dict[str, int]`` of counter
        # increments and the dispatcher folds them into the totals
        # generically. Behaviour for existing self-heal rules is
        # byte-identical (the registry adapters wrap the original
        # helpers — see ``_self_heal_*_counter`` shims above).
        healer = _TIER1_HEALERS.get(finding.rule)
        if healer is not None:
            try:
                heal_counters = healer(
                    finding,
                    project_key=project_key,
                    project_path=project_path,
                    config_path=config_path,
                ) or {}
            except Exception:  # noqa: BLE001
                logger.warning(
                    "audit.watchdog: tier-1 healer raised for %s",
                    finding.rule, exc_info=True,
                )
                heal_counters = {}
            for key, value in heal_counters.items():
                counters[key] = counters.get(key, 0) + int(value)
            # Tier-1 finishes the rule — no architect / operator dispatch.
            continue

        # #1524 — plan_missing_alert_churn is detection-only. The repair
        # is Part A (the deterministic precompute in the task-assignment
        # sweep). The finding + alert + audit-log row are the canary so
        # future-us notices when the regression returns; no architect
        # dispatch and no self-heal action.
        if finding.rule == RULE_PLAN_MISSING_ALERT_CHURN:
            counters["plan_missing_churn_detected"] += 1
            continue

        # #1546 — tier-3 operator dispatchable rules route to the
        # operator inbox via ``_maybe_dispatch_to_operator``. The
        # dispatcher returns "skipped" for non-operator rules so the
        # architect leg below still runs for tier-2.
        # #1553 — auto-promote: if the same root_cause_hash has hit
        # tier-3 K times in 24h, route to tier-4 instead. The tracker
        # is best-effort — failures fall back to tier-3 unchanged.
        if finding.rule in _OPERATOR_DISPATCHABLE_RULES:
            tracker = _build_tier4_tracker()
            promoted = False
            if tracker is not None:
                try:
                    if tracker.should_auto_promote(finding, now=now):
                        outcome = _dispatch_to_operator_tier4(
                            finding,
                            project_path=project_path,
                            now=now,
                            promotion_path="watchdog",
                            tracker=tracker,
                        )
                        if outcome == "dispatched":
                            counters["tier4_dispatches_sent"] += 1
                            promoted = True
                        elif outcome == "throttled":
                            counters["tier4_dispatches_throttled"] += 1
                            # Throttled means a tier-4 run is already
                            # active for this hash — skip the tier-3
                            # path so we don't double-dispatch.
                            promoted = True
                        elif outcome == "send_failed":
                            counters["tier4_dispatches_failed"] += 1
                            # Fall through to tier-3 so we don't drop
                            # the dispatch entirely.
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "tier4: auto-promote check raised for %s/%s",
                        finding.rule, finding.project, exc_info=True,
                    )

            if not promoted:
                # #1546 fix — thread ``config_path`` so alternate-config
                # heartbeat runs route the inbox task to the right
                # workspace DB (preserved across the #1553 tier-4 leg).
                op_outcome = _maybe_dispatch_to_operator(
                    finding,
                    project_path=project_path,
                    now=now,
                    config_path=config_path,
                )
                if op_outcome == "dispatched":
                    counters["operator_dispatches_sent"] += 1
                    # #1553 — record the dispatch in the tracker so the
                    # next time this hash appears we count it toward K.
                    if tracker is not None:
                        try:
                            tracker.record_tier3_dispatch(finding, now=now)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "tier4: record_tier3_dispatch raised",
                                exc_info=True,
                            )
                elif op_outcome == "throttled":
                    counters["operator_dispatches_throttled"] += 1
                elif op_outcome == "send_failed":
                    counters["operator_dispatches_failed"] += 1
            continue

        # #1414 — eligible findings get an architect dispatch on top
        # of the alert. Throttle window is owned by the audit log so
        # repeat dispatches are deduped across cadence-process restarts.
        # #1424 — for ``task_on_hold_stale`` we enrich the finding with
        # the reviewer's recent execution rows + inbox messages so the
        # architect's brief carries the rejection rationale, not just
        # the bare on_hold timestamp.
        dispatch_finding = _enrich_finding_metadata(
            finding,
            project_key=project_key,
            project_path=project_path,
            msg_store=msg_store,
        )
        outcome = _maybe_dispatch_to_architect(
            dispatch_finding,
            project_path=project_path,
            storage_closet_name=storage_closet_name,
            now=now,
        )
        if outcome == "dispatched":
            counters["dispatches_sent"] += 1
        elif outcome == "throttled":
            counters["dispatches_throttled"] += 1
        elif outcome == "send_failed":
            counters["dispatches_failed"] += 1
    return counters


def audit_watchdog_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadence handler entry point.

    Reads the audit log for every known project, runs the watchdog
    detectors, and routes every finding to an alert + a forensic
    audit event.

    Returns a dict with per-project + total counters for observability.
    """
    from pollypm.runtime_services import load_runtime_services

    config = _config_from_payload(payload or {})
    config_path_hint = (
        payload.get("config_path") if isinstance(payload, dict) else None
    )
    config_path = Path(config_path_hint) if config_path_hint else None

    services = load_runtime_services(config_path=config_path)
    now = datetime.now(UTC)

    # Liveness ping — emitted before scanning so that even if
    # scanning blows up the heartbeat-tick still lands in the log.
    emit_heartbeat_tick(metadata={"cadence": AUDIT_WATCHDOG_SCHEDULE})

    totals: dict[str, int] = {
        "projects_scanned": 0,
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
        "dispatches_sent": 0,
        "dispatches_throttled": 0,
        "dispatches_failed": 0,
        "advisor_duplicates_cancelled": 0,
        "advisor_duplicates_failed": 0,
        "plan_review_backfilled": 0,
        "plan_review_backfill_failed": 0,
        "legacy_db_shadows_migrated": 0,
        "legacy_db_shadows_failed": 0,
        "plan_missing_churn_detected": 0,
        # #1546 — new tier-1 + tier-3 totals.
        "worker_lane_spawned": 0,
        "worker_lane_failed": 0,
        "state_db_repaired": 0,
        "state_db_repair_failed": 0,
        "operator_dispatches_sent": 0,
        "operator_dispatches_throttled": 0,
        "operator_dispatches_failed": 0,
        # #1553 — tier-4 totals.
        "tier4_dispatches_sent": 0,
        "tier4_dispatches_throttled": 0,
        "tier4_dispatches_failed": 0,
        "tier4_demoted_cleared": 0,
        "tier4_budget_exhausted": 0,
    }

    try:
        seen_projects: set[str] = set()
        storage_closet_name = getattr(
            services, "storage_closet_name", None,
        )
        # #1519 — probe the canonical-vs-legacy DB shadow once per tick
        # so every project's scan can read its own entry without
        # re-walking the registry. Best-effort; failures return [].
        try:
            legacy_db_shadows = _gather_legacy_db_shadows(services=services)
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: legacy-db-shadow probe failed", exc_info=True,
            )
            legacy_db_shadows = []
        # #1524 — gather plan_missing alert.cleared events once per tick
        # for the churn detector. Single messages-table query per tick;
        # the per-project scan filters down to its own scope.
        try:
            plan_missing_clears = _gather_plan_missing_clears(
                services=services, now=now, config=config,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: plan_missing-clear probe failed", exc_info=True,
            )
            plan_missing_clears = []
        # #1546 — probe canonical-state.db presence once per tick.
        # The probe resolves the workspace-root DB via the work-service
        # resolver; we hand it ``services.config`` (when the runtime
        # services bundle exposes one) so test-isolated configs route
        # through the same source of truth.
        try:
            state_db_probes = _gather_state_db_probes(
                services=services,
                config=getattr(services, "config", None),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: state-db-probe failed", exc_info=True,
            )
            state_db_probes = []
        # #1554 HIGH-1 — accumulate the union of seen root_cause_hash
        # values across every project scan. The tier-4 sweep uses this
        # to demote rows whose underlying finding has resolved.
        seen_root_cause_hashes: set[str] = set()
        for project in services.known_projects or ():
            project_key = getattr(project, "key", None)
            if not project_key or project_key in seen_projects:
                continue
            seen_projects.add(project_key)
            project_path = getattr(project, "path", None)
            partial = _scan_one_project(
                project_key=project_key,
                project_path=Path(project_path) if project_path else None,
                msg_store=services.msg_store,
                state_store=services.state_store,
                now=now,
                config=config,
                storage_closet_name=storage_closet_name,
                legacy_db_shadows=legacy_db_shadows,
                plan_missing_clears=plan_missing_clears,
                state_db_probes=state_db_probes,
                config_path=config_path,
            )
            totals["projects_scanned"] += 1
            project_seen = partial.pop("_seen_root_cause_hashes", None)
            if isinstance(project_seen, set):
                seen_root_cause_hashes.update(project_seen)
            for k, v in partial.items():
                totals[k] = totals.get(k, 0) + v

        # #1553 — tier-4 demotion + budget-exhaustion sweep. Runs once
        # per cadence tick AFTER per-project scans. Pure side-effects on
        # the tier-4 tracker + audit log + product_state; idempotent
        # across ticks because every transition is gated on tracker
        # state. Best-effort — failure here does not break the cadence.
        # #1554 HIGH-1 — pass the seen-hashes union so a successful
        # tier-4 fix (finding gone) demotes cleanly without hitting the
        # 2h budget / terminal path.
        try:
            sweep_counters = _sweep_tier4_budget_and_demotion(
                services=services,
                now=now,
                seen_root_cause_hashes=seen_root_cause_hashes,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "tier4: budget/demotion sweep raised", exc_info=True,
            )
            sweep_counters = {}
        for k, v in sweep_counters.items():
            totals[k] = totals.get(k, 0) + int(v)
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: services.close raised", exc_info=True,
            )

    return {"outcome": "swept", **totals}
