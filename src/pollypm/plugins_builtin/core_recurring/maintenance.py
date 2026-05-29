"""Maintenance-style recurring handlers for core_recurring."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from .shared import (
    _close_msg_store,
    _load_config,
    _load_config_and_store,
    _open_msg_store,
    _resolve_config_path,
)


logger = logging.getLogger(__name__)

_PROACTIVE_FAILOVER_ACTIVE_ALERT = "proactive_failover_active"
_PROACTIVE_FAILOVER_NO_CAPACITY_ALERT = "proactive_failover_no_capacity"
_PROACTIVE_FAILOVER_FAILED_ALERT = "proactive_failover_failed"


def capacity_probe_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Probe capacity for every configured account."""
    with _load_config_and_store(payload) as (config, store):
        from pollypm.capacity import probe_all_accounts

        probes = probe_all_accounts(config, store)
        summary = {probe.account_name: probe.state.value for probe in probes}
        return {"probes": summary}


def account_usage_refresh_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh cached usage snapshots for configured accounts."""
    from pollypm.account_usage_sampler import refresh_all_account_usage

    config_path = _resolve_config_path(payload)
    account_names = payload.get("accounts")
    if not isinstance(account_names, list):
        account_names = None
    samples = refresh_all_account_usage(
        config_path,
        account_names=account_names,
    )
    config = _load_config(payload)
    msg_store = _open_msg_store(config)
    try:
        proactive = _run_proactive_controller_failover(
            config_path,
            config,
            msg_store=msg_store,
        )
    finally:
        _close_msg_store(msg_store)
    return {
        "sampled": len(samples),
        "accounts": {
            sample.account_name: {
                "health": sample.health,
                "remaining_pct": sample.remaining_pct,
                "reset_at": sample.reset_at,
            }
            for sample in samples
        },
        "proactive_failover": proactive,
    }


def _run_proactive_controller_failover(
    config_path: Path,
    config: Any,
    *,
    msg_store: Any | None,
    switcher: Any | None = None,
) -> dict[str, Any]:
    """Apply the soft controller failover decision after usage refresh."""
    from pollypm.capacity import evaluate_proactive_controller_failover

    current_account = _current_operator_account(config)
    decision = evaluate_proactive_controller_failover(
        config,
        None,
        current_account=current_account,
    )
    return _apply_proactive_controller_failover(
        config_path,
        config,
        decision,
        msg_store=msg_store,
        switcher=switcher,
    )


def _current_operator_account(config: Any) -> str:
    session = _operator_session(config)
    if session is None:
        return str(getattr(getattr(config, "pollypm", None), "controller_account", "") or "")
    try:
        from pollypm.storage.pg_sessions import get_session_runtime

        runtime = get_session_runtime(session.name)
    except Exception:  # noqa: BLE001
        runtime = None
    effective = getattr(runtime, "effective_account", None) if runtime is not None else None
    if effective:
        return str(effective)
    return str(getattr(session, "account", "") or getattr(config.pollypm, "controller_account", "") or "")


def _operator_session(config: Any) -> Any | None:
    sessions = getattr(config, "sessions", {}) or {}
    if "operator" in sessions:
        return sessions["operator"]
    for session in sessions.values():
        if getattr(session, "role", "") == "operator-pm":
            return session
    return None


def _apply_proactive_controller_failover(
    config_path: Path,
    config: Any,
    decision: Any,
    *,
    msg_store: Any | None,
    switcher: Any | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "action": decision.action,
        "from": decision.current_account,
        "to": decision.selected_account,
        "reason": decision.reason,
        "threshold": decision.threshold_pct,
        "candidates_evaluated": decision.candidates_evaluated,
    }
    if decision.action == "none":
        if (
            decision.current_account != decision.primary_account
            and decision.selected_account == decision.current_account
        ):
            _clear_proactive_failover_alerts(
                msg_store,
                alert_types=(
                    _PROACTIVE_FAILOVER_NO_CAPACITY_ALERT,
                    _PROACTIVE_FAILOVER_FAILED_ALERT,
                ),
            )
            _upsert_proactive_failover_active_alert(msg_store, decision)
        else:
            _clear_proactive_failover_alerts(msg_store)
        return summary

    if decision.action == "alert":
        _clear_proactive_failover_alerts(
            msg_store,
            alert_types=(
                _PROACTIVE_FAILOVER_ACTIVE_ALERT,
                _PROACTIVE_FAILOVER_FAILED_ALERT,
            ),
        )
        _upsert_proactive_failover_no_capacity_alert(
            msg_store,
            (
                f"Primary account {decision.primary_account} is at or above "
                f"{decision.threshold_pct}% usage, but no failover account is below "
                "that threshold."
            ),
        )
        _append_proactive_failover_event(
            msg_store,
            config=config,
            from_account=decision.primary_account,
            to_account=None,
            reason=decision.reason,
            threshold=decision.threshold_pct,
            current_account=decision.current_account,
            status="warn",
        )
        return summary

    target = decision.selected_account
    operator = _operator_session(config)
    if target is None or operator is None:
        summary["error"] = "No operator session or selected account available"
        _emit_proactive_failover_audit(
            config,
            from_account=decision.current_account,
            to_account=target,
            reason=decision.reason,
            threshold=decision.threshold_pct,
            current_account=decision.current_account,
            status="error",
            error=summary["error"],
        )
        _upsert_proactive_failover_failed_alert(msg_store, summary["error"])
        return summary

    try:
        _switch_operator_account(
            config_path,
            operator.name,
            target,
            switcher=switcher,
        )
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)
        _emit_proactive_failover_audit(
            config,
            from_account=decision.current_account,
            to_account=target,
            reason=decision.reason,
            threshold=decision.threshold_pct,
            current_account=decision.current_account,
            status="error",
            error=str(exc),
        )
        _upsert_proactive_failover_failed_alert(
            msg_store,
            f"Proactive controller failover to {target} failed: {exc}",
        )
        return summary

    _clear_proactive_failover_alerts(
        msg_store,
        alert_types=(
            _PROACTIVE_FAILOVER_NO_CAPACITY_ALERT,
            _PROACTIVE_FAILOVER_FAILED_ALERT,
        ),
    )
    if target == decision.primary_account:
        _clear_proactive_failover_alerts(
            msg_store,
            alert_types=(_PROACTIVE_FAILOVER_ACTIVE_ALERT,),
        )
    else:
        _upsert_proactive_failover_active_alert(msg_store, decision)
    _append_proactive_failover_event(
        msg_store,
        config=config,
        from_account=(
            decision.primary_account
            if decision.action == "switch"
            else decision.current_account
        ),
        to_account=target,
        reason=decision.reason,
        threshold=decision.threshold_pct,
        current_account=decision.current_account,
        status="ok",
    )
    summary["applied"] = True
    return summary


def _switch_operator_account(
    config_path: Path,
    session_name: str,
    account_name: str,
    *,
    switcher: Any | None,
) -> None:
    if switcher is not None:
        switcher(session_name, account_name)
        return
    from pollypm.service_api import PollyPMService

    PollyPMService(config_path).switch_session_account(session_name, account_name)


def _append_proactive_failover_event(
    msg_store: Any | None,
    *,
    config: Any,
    from_account: str,
    to_account: str | None,
    reason: str,
    threshold: int,
    current_account: str,
    status: str,
) -> None:
    _emit_proactive_failover_audit(
        config,
        from_account=from_account,
        to_account=to_account,
        reason=reason,
        threshold=threshold,
        current_account=current_account,
        status=status,
    )
    if msg_store is None:
        return
    try:
        msg_store.append_event(
            scope="pollypm",
            sender="account.failover",
            subject="account.failover.proactive",
            payload={
                "from": from_account,
                "to": to_account,
                "reason": reason,
                "threshold": threshold,
                "current": current_account,
                "message": (
                    f"Proactive account failover {from_account} -> "
                    f"{to_account or 'none'} ({reason}, threshold={threshold}%)"
                ),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account.usage_refresh: failed to append proactive failover event",
            exc_info=True,
        )


def _emit_proactive_failover_audit(
    config: Any,
    *,
    from_account: str,
    to_account: str | None,
    reason: str,
    threshold: int,
    current_account: str,
    status: str,
    error: str | None = None,
) -> None:
    try:
        from pollypm.audit.log import (
            EVENT_ACCOUNT_FAILOVER_PROACTIVE,
            emit as _audit_emit,
        )

        project_root = getattr(getattr(config, "project", None), "root_dir", None)
        metadata = {
            "from": from_account,
            "to": to_account,
            "reason": reason,
            "threshold": threshold,
            "current": current_account,
        }
        if error:
            metadata["error"] = error
        _audit_emit(
            event=EVENT_ACCOUNT_FAILOVER_PROACTIVE,
            project="_workspace",
            subject="operator",
            actor="account.usage_refresh",
            status=status,
            metadata=metadata,
            project_path=project_root,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account.usage_refresh: failed to emit proactive failover audit",
            exc_info=True,
        )


def _upsert_proactive_failover_active_alert(
    msg_store: Any | None,
    decision: Any,
) -> None:
    if msg_store is None:
        return
    try:
        msg_store.upsert_alert(
            "pollypm",
            _PROACTIVE_FAILOVER_ACTIVE_ALERT,
            "info",
            (
                f"Primary account {decision.primary_account} is at or above "
                f"{decision.threshold_pct}% usage; operator is using "
                f"{decision.selected_account or decision.current_account}."
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account.usage_refresh: failed to upsert proactive failover "
            "active alert",
            exc_info=True,
        )


def _upsert_proactive_failover_no_capacity_alert(
    msg_store: Any | None,
    message: str,
) -> None:
    if msg_store is None:
        return
    try:
        msg_store.upsert_alert(
            "pollypm",
            _PROACTIVE_FAILOVER_NO_CAPACITY_ALERT,
            "warn",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account.usage_refresh: failed to upsert proactive failover alert",
            exc_info=True,
        )


def _upsert_proactive_failover_failed_alert(
    msg_store: Any | None,
    message: str,
) -> None:
    if msg_store is None:
        return
    try:
        msg_store.upsert_alert(
            "pollypm",
            _PROACTIVE_FAILOVER_FAILED_ALERT,
            "warn",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account.usage_refresh: failed to upsert proactive failover "
            "failure alert",
            exc_info=True,
        )


def _clear_proactive_failover_alerts(
    msg_store: Any | None,
    *,
    alert_types: tuple[str, ...] = (
        _PROACTIVE_FAILOVER_ACTIVE_ALERT,
        _PROACTIVE_FAILOVER_NO_CAPACITY_ALERT,
        _PROACTIVE_FAILOVER_FAILED_ALERT,
    ),
) -> None:
    if msg_store is None:
        return
    for alert_type in alert_types:
        try:
            msg_store.clear_alert(
                "pollypm",
                alert_type,
                who_cleared="auto:account.usage_refresh",
            )
        except TypeError:
            try:
                msg_store.clear_alert("pollypm", alert_type)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "account.usage_refresh: clear proactive failover alert "
                    "failed",
                    exc_info=True,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "account.usage_refresh: clear proactive failover alert failed",
                exc_info=True,
            )


def transcript_ingest_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tail provider transcripts into the shared events ledger."""
    config = _load_config(payload)

    from pollypm.transcript_ingest import sync_transcripts_once

    sync_transcripts_once(config)
    return {"ok": True}


def db_vacuum_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an incremental vacuum against StateStore to reclaim freelist pages.

    No-op on the pg backend — autovacuum and the pg autovacuum daemon
    handle page reclamation, and StateStore.incremental_vacuum is a
    sqlite-only PRAGMA.
    """
    with _load_config_and_store(payload) as (_config, store):
        if store is None:
            return {"bytes_reclaimed": 0, "mb_reclaimed": 0.0, "skipped": "pg backend"}
        bytes_reclaimed = store.incremental_vacuum()
        mb_reclaimed = bytes_reclaimed / (1024 * 1024)
        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="db.vacuum",
                    payload={
                        "message": f"reclaimed {mb_reclaimed:.1f}MB",
                        "bytes_reclaimed": bytes_reclaimed,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return {"bytes_reclaimed": bytes_reclaimed, "mb_reclaimed": mb_reclaimed}


# Handlers without a registered timeout (or whose registry isn't reachable
# from the recurring sweep) get this safety floor before the 2× multiplier
# is applied. 600 s is generous — it's longer than every default-registered
# handler timeout, so we're never aggressive on an unknown handler.
_STUCK_CLAIMS_DEFAULT_TIMEOUT_SECONDS: float = 300.0
_STUCK_CLAIMS_FLOOR_SECONDS: float = 600.0


def _resolve_handler_timeouts() -> dict[str, float]:
    """Best-effort handler-name -> timeout_seconds map for the running host.

    Returns ``{}`` when the plugin host isn't reachable (e.g. in an
    isolated test that constructs a queue but no extension host). The
    sweep falls back to the 600 s safety floor in that case.
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path
        from pollypm.plugin_host import extension_host_for_root
    except Exception:  # noqa: BLE001
        # #1355: previously silent. Falling back to the 600s floor
        # for every handler is a real degradation (slower stuck-claim
        # recovery, no per-handler precision); log so an import-time
        # break doesn't silently widen the sweep's blast radius.
        logger.warning(
            "stuck_claims.sweep: handler-timeout imports failed; "
            "using fallback floor",
            exc_info=True,
        )
        return {}
    try:
        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        # The cached host is keyed on the project root_dir. Use the
        # config's parent as a stable key so a per-project sweep finds
        # the same registry as boot.
        host = extension_host_for_root(str(config_path.parent))
        registry = host.job_handler_registry()
    except Exception:  # noqa: BLE001
        # #1355: previously silent. An unreachable plugin host means
        # the sweep uses the 600s floor for every job. Log so a
        # broken host wiring stops hiding behind quiet degradation.
        logger.warning(
            "stuck_claims.sweep: plugin host unreachable; "
            "using fallback floor",
            exc_info=True,
        )
        return {}
    try:
        snapshot = registry.snapshot()
    except Exception:  # noqa: BLE001
        # #1355: previously silent. Same degradation as above.
        logger.warning(
            "stuck_claims.sweep: registry.snapshot() failed; "
            "using fallback floor",
            exc_info=True,
        )
        return {}
    result: dict[str, float] = {}
    for name, spec in snapshot.items():
        timeout = getattr(spec, "timeout_seconds", None)
        if isinstance(timeout, (int, float)) and timeout > 0:
            result[name] = float(timeout)
    return result


def stuck_claims_sweep_handler(
    payload: dict[str, Any],
    *,
    queue: Any | None = None,
    handler_timeouts: dict[str, float] | None = None,
    now: Any | None = None,
) -> dict[str, Any]:
    """Force-fail claimed jobs whose claim is past the per-handler cutoff (#1049).

    Cutoff = ``claimed_at + max(handler_timeout_seconds * 2, 600s)``. The
    2× factor accounts for the watchdog's existing handler-timeout
    window plus the lock-retry budget; the 600 s floor handles handlers
    without a registered spec (e.g. a stale row from a removed plugin).

    Idempotent — running twice in quick succession only re-fails jobs
    that are still past the cutoff. Jobs already moved back to
    ``queued`` (or to terminal ``failed``) by the first pass are
    invisible to the second.

    The ``queue`` / ``handler_timeouts`` / ``now`` keyword arguments
    are test seams — production callers pass only ``payload``.
    """
    from datetime import UTC, datetime, timedelta

    from pollypm.jobs import JobQueue

    resolved_now = now if isinstance(now, datetime) else datetime.now(UTC)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)

    timeouts = (
        dict(handler_timeouts)
        if handler_timeouts is not None
        else _resolve_handler_timeouts()
    )

    summary = {
        "scanned": 0,
        "recovered": 0,
        "failed_terminal": 0,
        "skipped_recent": 0,
        "errors": 0,
    }

    def _do_sweep(q: Any) -> dict[str, Any]:
        try:
            stuck = q.find_stuck_claims()
        except Exception:  # noqa: BLE001
            logger.debug(
                "stuck_claims.sweep: find_stuck_claims failed",
                exc_info=True,
            )
            return summary

        for job in stuck:
            summary["scanned"] += 1
            claimed_at = getattr(job, "claimed_at", None)
            if claimed_at is None:
                continue
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=UTC)

            timeout = timeouts.get(
                job.handler_name, _STUCK_CLAIMS_DEFAULT_TIMEOUT_SECONDS,
            )
            cutoff_seconds = max(
                float(timeout) * 2.0, _STUCK_CLAIMS_FLOOR_SECONDS,
            )
            cutoff = claimed_at + timedelta(seconds=cutoff_seconds)
            if resolved_now < cutoff:
                summary["skipped_recent"] += 1
                continue

            terminal = job.attempt >= job.max_attempts
            try:
                q.fail(
                    job.id,
                    "auto-recovered stuck claim",
                    retry=True,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "stuck_claims.sweep: fail(%s) raised",
                    job.id, exc_info=True,
                )
                summary["errors"] += 1
                continue

            if terminal:
                summary["failed_terminal"] += 1
            else:
                summary["recovered"] += 1

        return summary

    if queue is not None:
        return _do_sweep(queue)

    config = _load_config(payload)
    state_db = config.project.state_db
    with JobQueue(db_path=state_db) as q:
        return _do_sweep(q)


# ---------------------------------------------------------------------------
# #1815 — audit_watchdog liveness auto-heal probe
# ---------------------------------------------------------------------------

# Cadence handlers must fire on their schedule for the system to be safe.
# ``audit.watchdog`` is the most safety-critical of those — its 5 min sweep
# is what catches stuck_draft / task_progress_stale / role_session_missing /
# worker_session_dead_loop / cancellation_no_promotion / queue_without_motion /
# advisor-priming-gaps. When it silently stops firing the operator-visible
# surface (``pm alerts``, the rail badge) still looks healthy because
# everything else is alive. #1815 documents the wedge symptom: last
# ``heartbeat.tick`` 9+ hours stale while the heartbeat process itself is
# alive and respawning.
#
# The probe runs once a minute and:
#   1. Reads the freshest ``heartbeat.tick`` from the central audit tail.
#   2. If the gap exceeds ``LIVENESS_STALE_THRESHOLD_SECONDS`` (default 3x
#      the ``AUDIT_WATCHDOG_SCHEDULE``), force-recovers the queue
#      (clears orphaned ``claimed`` rows that may be holding the
#      ``audit.watchdog`` dedupe slot open) and re-enqueues the
#      cadence job so the watchdog re-arms within ~60 s of detection.
#   3. Emits an explicit ``audit_watchdog`` alert via the unified
#      message store so ``pm alerts`` shows the heal action — an
#      operator scanning the inbox sees "watchdog was wedged; auto-
#      healed at <ts>" instead of silent recovery.
#
# This is the prong-2 self-heal rule called out by
# ``project_heartbeat_cascade``: manual patches without a self-heal rule
# are incomplete.

# 3x the audit_watchdog schedule (5 min). A single missed fire is fine;
# three in a row is the wedge.
LIVENESS_STALE_THRESHOLD_SECONDS: float = 900.0
# Floor on the heal cadence so the auto-heal can never spin: at most one
# re-arm + alert per HEAL_THROTTLE_SECONDS. Keeps the probe idempotent
# under contention if the watchdog handler is genuinely broken (in which
# case the alert sticks around in ``pm alerts`` until the operator
# acknowledges it).
HEAL_THROTTLE_SECONDS: float = 300.0


def audit_watchdog_liveness_probe_handler(
    payload: dict[str, Any],
    *,
    queue: Any | None = None,
    now: Any | None = None,
    stale_threshold_seconds: float | None = None,
) -> dict[str, Any]:
    """Detect and auto-heal a wedged ``audit.watchdog`` scheduler (#1815).

    The ``queue`` / ``now`` / ``stale_threshold_seconds`` keyword
    arguments are test seams — production callers pass only ``payload``.

    Returns a summary dict with:

    * ``freshest_tick_ts``: ISO timestamp of the most recent
      ``heartbeat.tick`` event found in the central tail, or ``None``.
    * ``age_seconds``: how stale that event is relative to ``now``,
      or ``None`` when no tick exists.
    * ``stale``: bool — True iff age exceeds the threshold (or no
      tick exists at all).
    * ``action``: one of ``"none"``, ``"healed"``, ``"throttled"``.
    """
    from datetime import UTC, datetime

    from pollypm.audit.watchdog import (
        freshest_heartbeat_tick_ts,
    )
    from pollypm.jobs import JobQueue
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        AUDIT_WATCHDOG_HANDLER_NAME as _WATCHDOG_HANDLER_NAME,
    )

    resolved_now = now if isinstance(now, datetime) else datetime.now(UTC)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)

    threshold = (
        float(stale_threshold_seconds)
        if stale_threshold_seconds is not None
        else LIVENESS_STALE_THRESHOLD_SECONDS
    )

    freshest = freshest_heartbeat_tick_ts()
    if freshest is None:
        age_seconds: float | None = None
        stale = True
    else:
        age_seconds = (resolved_now - freshest).total_seconds()
        stale = age_seconds >= threshold

    summary: dict[str, Any] = {
        "freshest_tick_ts": freshest.isoformat() if freshest is not None else None,
        "age_seconds": age_seconds,
        "stale": stale,
        "action": "none",
        "threshold_seconds": threshold,
    }

    if not stale:
        return summary

    def _do_heal(q: Any) -> None:
        # 1. Release any orphaned ``claimed`` rows FIRST. The most
        # likely wedge under the new pg queue is that the previous
        # ``audit.watchdog`` claim is still held by a dead /
        # GC'd worker thread, and the dedupe-on-status='claimed'
        # short-circuit at JobQueue.enqueue blocks every new fire.
        #
        # #1829 ordering fix: we used to throttle before this step,
        # but ``has_recent_or_active_dedupe`` returns true on any
        # ``claimed`` row — including the orphaned one we are about
        # to recover — so the throttle would skip recovery and the
        # wedge would persist. Recovering first lets the throttle
        # see the post-heal queue state.
        try:
            recovered, pruned = q.recover_orphaned_claims()
            summary["recovered_claims"] = recovered
            summary["pruned_claims"] = pruned
        except Exception:  # noqa: BLE001
            logger.warning(
                "audit_watchdog.liveness_probe: "
                "recover_orphaned_claims failed",
                exc_info=True,
            )
            summary["recovered_claims"] = 0
            summary["pruned_claims"] = 0

        # 2. Anti-loop throttle: skip the re-enqueue / alert path if
        # we *already* re-enqueued a heal inside the last
        # HEAL_THROTTLE_SECONDS. We only consider rows that point at a
        # prior heal attempt (``status IN ('queued', 'done')`` enqueued
        # since the window), not a stale ``claimed`` row — that was
        # the #1829 bug. ``has_recent_or_active_dedupe`` still returns
        # true on stale ``claimed`` rows, so we additionally require
        # that ``recovered_claims == 0`` for this iteration (if we
        # just recovered something, the previous "active dedupe" was
        # the wedge we're recovering, not a recent legitimate heal).
        recently = getattr(q, "has_recent_or_active_dedupe", None)
        if callable(recently) and summary.get("recovered_claims", 0) == 0:
            since = resolved_now - timedelta(seconds=HEAL_THROTTLE_SECONDS)
            try:
                if recently(_WATCHDOG_HANDLER_NAME, since=since):
                    summary["action"] = "throttled"
                    return
            except Exception:  # noqa: BLE001
                logger.debug(
                    "audit_watchdog.liveness_probe: "
                    "has_recent_or_active_dedupe failed",
                    exc_info=True,
                )

        # 3. Re-enqueue the cadence job with the canonical dedupe
        # key. If a prior row is queued / claimed at this point the
        # enqueue is a no-op (returns the existing id), which is the
        # correct behaviour — the heal already cleared the wedge,
        # and a clean schedule on the cadence is enough.
        try:
            job_id = q.enqueue(
                _WATCHDOG_HANDLER_NAME,
                {},
                dedupe_key=_WATCHDOG_HANDLER_NAME,
            )
            summary["enqueued_job_id"] = int(job_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "audit_watchdog.liveness_probe: re-enqueue failed",
                exc_info=True,
            )

        summary["action"] = "healed"

        # 4. Audit + alert so the heal action is observable. Audit
        # emit is best-effort; the alert is the operator-visible
        # surface.
        try:
            from pollypm.audit.log import emit as _audit_emit

            _audit_emit(
                event="audit_watchdog.liveness_probe.healed",
                project="",
                subject="audit_watchdog",
                actor="audit_watchdog.liveness_probe",
                status="warn",
                metadata={
                    "freshest_tick_ts": summary["freshest_tick_ts"],
                    "age_seconds": age_seconds,
                    "threshold_seconds": threshold,
                    "recovered_claims": summary.get("recovered_claims", 0),
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit_watchdog.liveness_probe: audit emit failed",
                exc_info=True,
            )

        _emit_liveness_probe_alert(
            payload=payload,
            age_seconds=age_seconds,
            freshest_ts=summary["freshest_tick_ts"],
            threshold_seconds=threshold,
        )

    if queue is not None:
        _do_heal(queue)
        return summary

    try:
        config = _load_config(payload)
        state_db = config.project.state_db
    except Exception:  # noqa: BLE001
        # If config can't be resolved, we can't construct the queue
        # without explicit injection. Still record the stale signal
        # — the audit emit alone is useful for forensics.
        logger.warning(
            "audit_watchdog.liveness_probe: config resolution failed; "
            "cannot heal",
            exc_info=True,
        )
        summary["action"] = "config_unavailable"
        return summary

    with JobQueue(db_path=state_db) as q:
        _do_heal(q)
    return summary


def _emit_liveness_probe_alert(
    *,
    payload: dict[str, Any],
    age_seconds: float | None,
    freshest_ts: str | None,
    threshold_seconds: float,
) -> None:
    """Best-effort: upsert a ``watchdog_silent`` alert into ``pm alerts``.

    The alert is keyed on a synthetic session name so repeat heals
    fold into the same row instead of accumulating duplicates. We
    use ``upsert_alert`` (the same channel the watchdog itself uses
    for its findings) so ``pm alerts`` lists it under the same
    actor surface.
    """
    try:
        config = _load_config(payload)
    except Exception:  # noqa: BLE001
        return
    msg_store = _open_msg_store(config)
    if msg_store is None:
        return
    upsert = getattr(msg_store, "upsert_alert", None)
    if not callable(upsert):
        _close_msg_store(msg_store)
        return
    if age_seconds is None:
        body = (
            f"audit_watchdog liveness probe: no heartbeat.tick events "
            f"found in central audit tail. Threshold {threshold_seconds:.0f}s. "
            f"Auto-heal re-enqueued the cadence job."
        )
    else:
        body = (
            f"audit_watchdog wedged: last heartbeat.tick was "
            f"{age_seconds:.0f}s ago ({freshest_ts}); threshold "
            f"{threshold_seconds:.0f}s. Auto-heal recovered orphan claims "
            f"and re-enqueued the cadence job."
        )
    try:
        upsert(
            "audit_watchdog/liveness",
            "watchdog_silent",
            "error",
            body,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit_watchdog.liveness_probe: upsert_alert failed",
            exc_info=True,
        )
    finally:
        _close_msg_store(msg_store)


AUDIT_EVENT_SUBJECTS: frozenset[str] = frozenset({
    "task.approved", "task.rejected", "task.done", "task.claimed",
    "task.queued", "plan.approved", "inbox.message.created", "launch",
    "recovered", "recovery_prompt", "state_drift",
    "persona_swap_detected", "alert", "escalated",
})
OPERATIONAL_EVENT_SUBJECTS: frozenset[str] = frozenset({
    "lease", "stop", "send_input", "nudge", "ran", "processed",
    "stabilize_failed", "delivery",
})
HIGH_VOLUME_EVENT_SUBJECTS: frozenset[str] = frozenset({
    "heartbeat", "heartbeat_error", "token_ledger", "scheduled",
})


def events_retention_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply tiered retention to ``messages WHERE type='event'`` (#267 / #342)."""
    from datetime import datetime, timedelta, timezone

    with _load_config_and_store(payload) as (config, _store):
        settings = config.events
        now = datetime.now(timezone.utc)
        msg_store = _open_msg_store(config)
        if msg_store is None:
            return {
                "deleted_audit": 0,
                "deleted_operational": 0,
                "deleted_high_volume": 0,
                "deleted_default": 0,
                "total": 0,
            }

        try:
            audit_cutoff = now - timedelta(days=settings.audit_retention_days)
            operational_cutoff = now - timedelta(
                days=settings.operational_retention_days,
            )
            high_volume_cutoff = now - timedelta(
                days=settings.high_volume_retention_days,
            )
            default_cutoff = now - timedelta(days=settings.default_retention_days)

            deleted_audit = 0
            deleted_operational = 0
            deleted_high_volume = 0
            deleted_default = 0

            for subject in AUDIT_EVENT_SUBJECTS:
                deleted_audit += _prune_event_subject(
                    msg_store, subject, audit_cutoff,
                )
            for subject in OPERATIONAL_EVENT_SUBJECTS:
                deleted_operational += _prune_event_subject(
                    msg_store, subject, operational_cutoff,
                )
            for subject in HIGH_VOLUME_EVENT_SUBJECTS:
                deleted_high_volume += _prune_event_subject(
                    msg_store, subject, high_volume_cutoff,
                )

            known = (
                AUDIT_EVENT_SUBJECTS
                | OPERATIONAL_EVENT_SUBJECTS
                | HIGH_VOLUME_EVENT_SUBJECTS
            )
            try:
                # #1820 — typed prune_messages so pg and sqlite share
                # one delete shape; the legacy execute(_delete(...))
                # form raised on PgStore and was silently swallowed.
                deleted_default = int(
                    msg_store.prune_messages(
                        type="event",
                        subject_not_in=tuple(known),
                        older_than=default_cutoff,
                    )
                    or 0
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "events.retention_sweep: default-tier delete failed",
                    exc_info=True,
                )
                deleted_default = 0

            total = (
                deleted_audit
                + deleted_operational
                + deleted_high_volume
                + deleted_default
            )
            counts = {
                "deleted_audit": deleted_audit,
                "deleted_operational": deleted_operational,
                "deleted_high_volume": deleted_high_volume,
                "deleted_default": deleted_default,
                "total": total,
            }

            if total > 0:
                try:
                    msg_store.record_event(
                        scope="system",
                        sender="system",
                        subject="events.retention_sweep",
                        payload={
                            "message": (
                                f"deleted {total} events "
                                f"(audit={deleted_audit}, "
                                f"operational={deleted_operational}, "
                                f"high_volume={deleted_high_volume}, "
                                f"default={deleted_default})"
                            ),
                            **counts,
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "events.retention_sweep: audit-event emit failed",
                        exc_info=True,
                    )
        finally:
            _close_msg_store(msg_store)

        return counts


def _prune_event_subject(msg_store: Any, subject: str, cutoff: Any) -> int:
    """Delete ``type='event'`` rows matching ``subject`` older than ``cutoff``.

    #1820 — typed prune_messages so pg + sqlite share one shape; the
    previous execute(_delete(...)) raised on PgStore and was swallowed.
    """
    try:
        return int(
            msg_store.prune_messages(
                type="event",
                subject=subject,
                older_than=cutoff,
            )
            or 0
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "events.retention_sweep: delete failed for subject=%s",
            subject, exc_info=True,
        )
        return 0


def memory_ttl_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop expired memory_entries (TTL in the past)."""
    with _load_config_and_store(payload) as (_config, store):
        if store is not None:
            deleted = store.sweep_expired_memory_entries()
        else:
            from pollypm.storage.pg_memory import sweep_expired_memory_entries

            deleted = sweep_expired_memory_entries()
        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="memory.ttl_sweep",
                    payload={
                        "message": f"dropped {deleted} expired entries",
                        "deleted": deleted,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return {"deleted": deleted}


def _discover_agent_worktrees_root() -> Path | None:
    """Walk up from cwd + this module's location to find a repo whose
    ``.claude/worktrees/`` dir exists (#1965).

    Mirrors :func:`pollypm.doctor._agent_worktree_dirs` so the prune
    handler's target tree matches what the ``agent-worktree-count``
    alert measures. Without this, the cron-tick path resolves
    ``repo_root`` from the global config's ``project.root_dir`` —
    typically ``~/.pollypm/`` for the ``rail_daemon`` — which has no
    ``.claude/worktrees/`` subtree, so the handler bails before ever
    inspecting the dev repo where worktrees actually accumulate.
    """
    here = Path(__file__).resolve()
    seen: set[Path] = set()
    for start in (Path.cwd().resolve(), here):
        for parent in (start, *start.parents):
            if parent in seen:
                continue
            seen.add(parent)
            if (parent / ".claude" / "worktrees").is_dir():
                return parent
    return None


def agent_worktree_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Prune stale Claude Code harness agent worktrees under ``.claude/worktrees/``."""
    import subprocess
    import time

    hint = payload.get("project_root") if isinstance(payload, dict) else None
    if hint:
        repo_root = Path(hint)
    else:
        # #1965: prefer a walk-up discovery so the cron-tick path
        # targets the actual dev repo. Fall back to config.root_dir
        # only when discovery fails — preserves prior behavior for
        # callers who have set that up correctly.
        discovered = _discover_agent_worktrees_root()
        if discovered is not None:
            repo_root = discovered
        else:
            config = _load_config(payload)
            repo_root = config.project.root_dir

    worktrees_dir = repo_root / ".claude" / "worktrees"
    if not worktrees_dir.is_dir():
        return {"pruned": 0, "skipped_active": 0, "warned_stale": 0, "errors": 0}

    now = time.time()
    one_hour = 3600.0
    seven_days = 7 * 86400.0

    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=False,
        )

    merged_local = _git(repo_root, "branch", "--merged", "main")
    merged_remote = _git(repo_root, "branch", "-r", "--merged", "origin/main")
    merged_names: set[str] = set()
    for proc in (merged_local, merged_remote):
        if proc.returncode != 0:
            continue
        for raw in proc.stdout.splitlines():
            name = raw.strip().lstrip("*+").strip()
            if not name or name.startswith("("):
                continue
            if name.startswith("origin/"):
                name = name[len("origin/"):]
            merged_names.add(name)

    def _branch_content_in_main(branch: str) -> bool:
        """Return True when every commit on ``branch`` already exists in
        ``main`` by patch — covers squash-merged and cherry-picked
        branches that ``--merged main`` misses (#1066).
        """
        proc = _git(repo_root, "cherry", "main", branch)
        if proc.returncode != 0:
            return False
        # ``git cherry`` prefixes lines with ``+`` (commit not in upstream)
        # or ``-`` (already applied). All ``-`` (or empty output) means the
        # branch is fully content-merged.
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("+"):
                return False
        return True

    def _remove_worktree(wt_path: Path) -> bool:
        """Remove a worktree, unlocking first if it's locked (#1066).

        Claude Code harness leaves agent worktrees locked with a pid
        reason; the locking pid is long gone by the time the hourly
        prune fires, but ``git worktree remove --force`` still refuses.
        Unlock + force is the documented escape hatch.
        """
        proc = _git(repo_root, "worktree", "remove", "--force", str(wt_path))
        if proc.returncode == 0:
            return True
        # Try unlock + retry once. Locked worktrees emit
        # "fatal: cannot remove a locked working tree" on stderr.
        if "locked" in (proc.stderr or "").lower():
            _git(repo_root, "worktree", "unlock", str(wt_path))
            retry = _git(repo_root, "worktree", "remove", "--force", str(wt_path))
            if retry.returncode == 0:
                return True
        return False

    pruned = 0
    skipped_active = 0
    warned_stale = 0
    errors = 0

    for wt in sorted(worktrees_dir.glob("agent-*")):
        if not wt.is_dir():
            continue
        try:
            mtime = wt.stat().st_mtime
            age = now - mtime
            if age < one_hour:
                skipped_active += 1
                continue

            branch_proc = _git(wt, "branch", "--show-current")
            if branch_proc.returncode != 0:
                errors += 1
                continue
            branch = branch_proc.stdout.strip()
            if not branch:
                errors += 1
                continue

            is_merged = (
                branch in merged_names
                or _branch_content_in_main(branch)
            )
            if is_merged:
                if not _remove_worktree(wt):
                    errors += 1
                    continue
                _git(repo_root, "branch", "-D", branch)
                pruned += 1
            elif age > seven_days:
                logger.warning(
                    "agent_worktree.prune: stale unmerged worktree %s "
                    "(branch=%s, age_days=%.1f) — leaving in place",
                    wt, branch, age / 86400.0,
                )
                warned_stale += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "agent_worktree.prune: error processing %s", wt, exc_info=True,
            )
            errors += 1

    _git(repo_root, "worktree", "prune")

    return {
        "pruned": pruned,
        "skipped_active": skipped_active,
        "warned_stale": warned_stale,
        "errors": errors,
    }


def log_rotate_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Rotate + prune oversized log files under ``config.project.logs_dir``."""
    import gzip
    import os
    import re
    import shutil
    import time

    logs_dir_hint = payload.get("logs_dir") if isinstance(payload, dict) else None
    size_override = payload.get("rotate_size_mb") if isinstance(payload, dict) else None
    keep_override = payload.get("rotate_keep") if isinstance(payload, dict) else None

    if logs_dir_hint is not None:
        logs_dir = Path(logs_dir_hint)
        rotate_size_mb = int(size_override) if size_override is not None else 20
        rotate_keep = int(keep_override) if keep_override is not None else 3
    else:
        config = _load_config(payload)
        logs_dir = config.project.logs_dir
        rotate_size_mb = (
            int(size_override) if size_override is not None
            else config.logging.rotate_size_mb
        )
        rotate_keep = (
            int(keep_override) if keep_override is not None
            else config.logging.rotate_keep
        )

    if not logs_dir.is_dir():
        return {"rotated": 0, "deleted": 0, "errors": 0}

    threshold_bytes = max(1, rotate_size_mb) * 1024 * 1024
    rotated = 0
    deleted = 0
    errors = 0
    rotation_re = re.compile(r"^(?P<base>.+)\.log\.(?P<ts>\d+)\.gz$")

    # #1066: walk subdirectories too. PollyPM tmux pipe-pane targets land
    # under ``<logs_dir>/<session_slug>/<window>.log`` — a flat glob over
    # ``logs_dir/*.log`` never sees them, so rotation silently no-ops
    # while subdir log files balloon to hundreds of MB.
    for log_path in sorted(logs_dir.rglob("*.log")):
        if not log_path.is_file():
            continue
        try:
            size = log_path.stat().st_size
        except OSError:
            errors += 1
            continue
        if size <= threshold_bytes:
            continue
        ts = int(time.time())
        rotated_path = log_path.with_suffix(f".log.{ts}")
        bump = 0
        while rotated_path.exists():
            bump += 1
            rotated_path = log_path.with_suffix(f".log.{ts}.{bump}")
        try:
            os.rename(log_path, rotated_path)
            log_path.touch()
        except OSError:
            logger.debug(
                "log.rotate: rename failed for %s", log_path, exc_info=True,
            )
            errors += 1
            continue
        gz_path = rotated_path.with_suffix(rotated_path.suffix + ".gz")
        try:
            with open(rotated_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            rotated_path.unlink()
            rotated += 1
        except OSError:
            logger.debug(
                "log.rotate: gzip failed for %s", rotated_path, exc_info=True,
            )
            errors += 1
            continue

    # Group existing rotations by parent dir + base name so retention
    # applies per-log-stream (don't mix worker-foo and worker-bar even if
    # they sit in the same directory, and treat per-session subdirs
    # independently).
    by_base: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    for gz in logs_dir.rglob("*.log.*.gz"):
        m = rotation_re.match(gz.name)
        if not m:
            continue
        try:
            ts_val = int(m.group("ts"))
        except ValueError:
            continue
        by_base.setdefault((gz.parent, m.group("base")), []).append((ts_val, gz))

    for _key, entries in by_base.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        for _ts_val, gz_path in entries[rotate_keep:]:
            try:
                gz_path.unlink()
                deleted += 1
            except OSError:
                logger.debug(
                    "log.rotate: delete failed for %s", gz_path, exc_info=True,
                )
                errors += 1

    return {"rotated": rotated, "deleted": deleted, "errors": errors}


def cockpit_socket_reap_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Periodically reap stale ``cockpit_inputs/*.sock`` whose owner PID is dead.

    The bootstrap-time call from ``Supervisor._bootstrap_clear_markers``
    only fires once per ``pm up``. A long-lived cockpit that survives many
    per-task worker crash + restart cycles accumulates stale sockets
    (one per crashed pane) until shutdown (#1592). Running the reaper as
    a recurring handler on the ``rail_daemon`` / heartbeat thread keeps
    the directory bounded across the lifetime of a single boot.

    Safe to run concurrently with live cockpits and panes: the reaper
    only unlinks entries whose name encodes a PID that no longer exists,
    so a live bridge's socket (whose owner PID is alive by definition)
    is never touched. The same audit + log surfaces fire as at bootstrap.

    ``payload`` accepts an optional ``base_dir`` override (tests). When
    absent, the active config's ``project.base_dir`` is used.
    """
    from pollypm.cockpit_socket_reaper import reap_stale_cockpit_sockets

    base_override = payload.get("base_dir") if isinstance(payload, dict) else None
    if base_override:
        base_dir = Path(base_override)
    else:
        config = _load_config(payload)
        base_dir = config.project.base_dir
    reaped = reap_stale_cockpit_sockets(base_dir)
    return {"reaped": len(reaped)}


def cockpit_pane_reap_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Reap old cockpit-pane processes that are no longer live tmux panes."""
    from pollypm.cockpit_pane_reaper import reap_orphan_cockpit_panes

    raw_min_age = payload.get("min_age_s") if isinstance(payload, dict) else None
    try:
        min_age_s = int(raw_min_age) if raw_min_age is not None else 300
    except (TypeError, ValueError):
        min_age_s = 300
    reaped = reap_orphan_cockpit_panes(
        min_age_s=min_age_s,
        protect_live_tmux_panes=True,
    )
    return {"reaped": len(reaped)}


def notification_staging_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop flushed + silent notification_staging rows older than 30d."""
    from pollypm.work import create_work_service

    with _load_config_and_store(payload) as (_config, store):
        retain_days = int(payload.get("retain_days") or 30)
        # #1369 / #savethenovel-followup: the legacy fallback here was
        # ``_config.project.state_db`` — the *messages-side* DB. The
        # ``notification_staging`` table lives on the work-side DB, so
        # falling through to ``state_db`` would silently stamp the
        # work schema onto the messages DB on a fresh install. Route
        # through the work factory so the canonical resolver picks
        # the workspace-root work DB regardless of which fallback the
        # store ended up at.
        store_path = getattr(store, "path", None)
        if store_path is not None:
            svc_ctx = create_work_service(db_path=store_path)
        else:
            svc_ctx = create_work_service(config=_config)
        with svc_ctx as svc:
            summary = svc.prune_staged_notifications(retain_days=retain_days)

        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="notification_staging.prune",
                    payload={
                        "message": (
                            f"pruned {summary['flushed_pruned']} flushed + "
                            f"{summary['silent_pruned']} silent rows "
                            f"(>{retain_days}d)"
                        ),
                        **summary,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return summary
