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
    Finding,
    RULE_DUPLICATE_ADVISOR_TASKS,
    RULE_LEGACY_DB_SHADOW,
    RULE_PLAN_MISSING_ALERT_CHURN,
    RULE_PLAN_REVIEW_MISSING,
    RULE_ROLE_SESSION_MISSING,
    RULE_STUCK_DRAFT,
    RULE_TASK_ON_HOLD_STALE,
    RULE_TASK_PROGRESS_STALE,
    RULE_TASK_REVIEW_STALE,
    RULE_WORKER_SESSION_DEAD_LOOP,
    WATCHDOG_ALERT_TYPE,
    WatchdogConfig,
    emit_escalation_dispatched,
    emit_finding,
    emit_heartbeat_tick,
    format_finding_message,
    format_unstick_brief,
    scan_project,
    was_recently_dispatched,
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
_DISPATCHABLE_RULES: frozenset[str] = frozenset({
    # #1440 — stale drafts are architect-originated planning stalls. Alerting
    # alone leaves them parked forever; dispatch the architect to queue,
    # cancel, or rewrite them.
    RULE_STUCK_DRAFT,
    RULE_TASK_REVIEW_STALE,
    RULE_TASK_PROGRESS_STALE,
    RULE_ROLE_SESSION_MISSING,
    RULE_WORKER_SESSION_DEAD_LOOP,
    # #1424 — on_hold escalation lands in the architect's pane with the
    # reviewer's rationale folded into the brief. Default first responder
    # is the architect; only the architect calls ``pm notify`` if the
    # issue genuinely needs human judgement.
    RULE_TASK_ON_HOLD_STALE,
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
) -> dict[str, int]:
    """Scan one project and route every finding. Returns counters."""
    counters = {
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
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: scan_project(%s) failed",
            project_key, exc_info=True,
        )
        return counters

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
        # #1510 — duplicate-advisor cleanup is a self-healing action,
        # not an architect dispatch. The fix is mechanical (cancel all
        # but the newest), idempotent across ticks, and doesn't need a
        # human in the loop, so we run it inline here.
        if finding.rule == RULE_DUPLICATE_ADVISOR_TASKS:
            heal = _self_heal_duplicate_advisor_tasks(
                finding,
                project_key=project_key,
                project_path=project_path,
            )
            counters["advisor_duplicates_cancelled"] += heal["cancelled"]
            counters["advisor_duplicates_failed"] += heal["failed"]
            # Skip dispatch — this rule is auto-healed, not architect-routed.
            continue
        # #1511 — plan_review_missing findings are self-healing: the
        # cadence handler emits the missing inbox row via the same
        # code path the post-done hook uses (Part A). Idempotent because
        # ``emit_plan_review_for_task`` re-checks the messages table
        # before writing. No architect dispatch needed; the user sees
        # the backfilled card directly in their inbox.
        if finding.rule == RULE_PLAN_REVIEW_MISSING:
            backfilled = _backfill_plan_review_for_finding(
                finding,
                project_key=project_key,
                project_path=project_path,
            )
            if backfilled is not None:
                counters["plan_review_backfilled"] += 1
            else:
                counters["plan_review_backfill_failed"] += 1
            # Skip the dispatch path for this rule — the architect has
            # nothing to unstick; the watchdog itself just fixed it.
            continue
        # #1519 — legacy_db_shadow findings are self-healing: drain the
        # legacy per-project DB into canonical via ``migrate_one``. The
        # helper is idempotent (rows already in canonical are skipped,
        # source archived to ``state.db.legacy-1004`` only after a clean
        # copy), so a re-run on an already-healed workspace is a no-op.
        # No architect dispatch — the action is mechanical.
        if finding.rule == RULE_LEGACY_DB_SHADOW:
            heal = _self_heal_legacy_db_shadow(
                finding,
                project_key=project_key,
                project_path=project_path,
            )
            counters["legacy_db_shadows_migrated"] += heal["migrated"]
            counters["legacy_db_shadows_failed"] += heal["failed"]
            continue
        # #1524 — plan_missing_alert_churn is detection-only. The repair
        # is Part A (the deterministic precompute in the task-assignment
        # sweep). The finding + alert + audit-log row are the canary so
        # future-us notices when the regression returns; no architect
        # dispatch and no self-heal action.
        if finding.rule == RULE_PLAN_MISSING_ALERT_CHURN:
            counters["plan_missing_churn_detected"] += 1
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

    totals = {
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
            )
            totals["projects_scanned"] += 1
            for k, v in partial.items():
                totals[k] += v
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: services.close raised", exc_info=True,
            )

    return {"outcome": "swept", **totals}
