"""Built-in recurring handlers migrated from the old heartbeat loop.

Previously, the supervisor dispatched recurring work (inbox sweep, capacity
probe, session health sweep, transcript ingest, alert GC) on each tick via
direct function calls. Track 7 moves these onto the durable roster + job
queue so the heartbeat's entire responsibility is to enqueue and a separate
worker pool drains the queue.

Handlers live as module-level callables taking a payload dict; plugins
register them via ``register_handlers`` and register cadence via
``register_roster``. See issue #164.

Handlers must be self-sufficient — they receive only a JSON-serializable
payload, so each one loads the shared PollyPM config internally. The payload
may carry per-invocation hints (e.g. ``project_root`` overrides).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pollypm.plugin_api.v1 import Capability, JobHandlerAPI, PollyPMPlugin, RosterAPI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers — each is a standalone callable, tolerant of partial config.
# ---------------------------------------------------------------------------


def _load_config_and_store(payload: dict[str, Any]):
    """Open the config + state store for a handler invocation.

    Handlers accept an optional ``config_path`` override in ``payload`` so
    tests (and alt installations) can target a non-default config. Falls
    back to the global default discovery.

    Returns ``(config, store)``; the store is closed by caller via
    ``finally: store.close()`` — but for our handlers we use short-lived
    stores that exit with the function so garbage collection handles it.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = Path(override) if override else resolve_config_path(DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise RuntimeError(
            f"PollyPM config not found at {config_path}; cannot run recurring handler"
        )
    config = load_config(config_path)

    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)
    return config, store


def session_health_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one round of session health classification.

    Mirrors the supervisor's Phase 2 "fast synchronous sweep" — builds the
    ``SupervisorHeartbeatAPI``, invokes the configured heartbeat backend,
    collects alerts. Returns a small summary.

    The supervisor still owns the tmux-touching pieces, so this handler
    instantiates a transient ``Supervisor`` bound to the current config.
    Works for the co-located single-process setup; plugin overlays can
    replace this with a network-aware implementation.
    """
    config, _store = _load_config_and_store(payload)

    # Late import to avoid a supervisor import cycle at plugin load.
    from pollypm.supervisor import Supervisor

    supervisor = Supervisor(config)
    alerts = supervisor.run_heartbeat(snapshot_lines=int(payload.get("snapshot_lines", 200) or 200))
    return {"alerts_raised": len(alerts)}


def capacity_probe_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Probe capacity for every configured account."""
    config, store = _load_config_and_store(payload)

    from pollypm.capacity import probe_all_accounts

    probes = probe_all_accounts(config, store)
    summary = {probe.account_name: probe.state.value for probe in probes}
    return {"probes": summary}


def transcript_ingest_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tail provider transcripts into the shared events ledger."""
    config, _store = _load_config_and_store(payload)

    from pollypm.transcript_ingest import sync_transcripts_once

    sync_transcripts_once(config)
    return {"ok": True}


def work_progress_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan in_progress tasks for staleness and emit resume pings (#249).

    Complements the 10s ``session.health_sweep`` — at a lower 5-min
    cadence we iterate every ``in_progress`` task whose current actor is
    a machine (``actor_type != user``) and check:

      * The claimant session exists.
      * The claimant session is idle (``is_turn_active == False``).
      * The session hasn't recorded an event in the last 30 minutes.

    When all three hold we re-emit the assignment event through
    ``task_assignment_notify``'s ``notify()`` helper — the plugin's
    existing 30-min dedupe table (``task_notifications``) guarantees at
    most one ping per (session, task) per 30 minutes.

    Returns a small summary so the job runner records useful output.
    Never raises on a per-task failure — the sweep continues.
    """
    from datetime import UTC, datetime, timedelta

    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _build_event_for_task,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        DEDUPE_WINDOW_SECONDS,
        load_runtime_services,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        notify as _notify,
    )
    from pollypm.recovery.state_reconciliation import (
        reconcile_expected_advance,
    )
    from pollypm.work.models import ActorType
    from pollypm.work.task_assignment import SessionRoleIndex

    # 30 min — mirrors the stuck_on_task threshold + dedupe window.
    STALE_THRESHOLD_SECONDS = int(
        payload.get("stale_threshold_seconds") or 1800,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    work = services.work_service
    state_store = services.state_store
    session_svc = services.session_service

    if work is None:
        return {"outcome": "skipped", "reason": "no_work_service"}

    considered = 0
    pinged = 0
    skipped_active_turn = 0
    skipped_recent_event = 0
    skipped_no_session = 0
    deduped = 0
    # #296 — state-drift counters. ``drift_detected`` is the number of
    # tasks whose observable deliverables outpace their flow node on
    # this sweep; ``drift_alerted`` is the subset where we actually
    # raised a fresh alert (the rest were deduped by upsert_alert's
    # per-type uniqueness guard).
    drift_detected = 0
    drift_alerted = 0

    try:
        try:
            tasks = work.list_tasks(work_status="in_progress")
        except Exception:  # noqa: BLE001
            logger.debug(
                "work.progress_sweep: list_tasks(in_progress) failed",
                exc_info=True,
            )
            return {"outcome": "failed", "reason": "list_tasks_error"}

        # Index: resolve each event's target session handle.
        index = (
            SessionRoleIndex(session_svc, work_service=work)
            if session_svc is not None else None
        )

        now = datetime.now(UTC)
        for task in tasks:
            try:
                event = _build_event_for_task(work, task)
            except Exception:  # noqa: BLE001
                continue
            if event is None:
                continue
            # Only machine actors — we don't ping humans.
            if event.actor_type is ActorType.HUMAN:
                continue
            considered += 1

            # Resolve the target session.
            handle = None
            if index is not None:
                try:
                    handle = index.resolve(
                        event.actor_type, event.actor_name, event.project,
                    )
                except Exception:  # noqa: BLE001
                    handle = None
            if handle is None:
                skipped_no_session += 1
                continue
            target_name = getattr(handle, "name", "")
            if not target_name:
                skipped_no_session += 1
                continue

            # Skip actively-turning sessions — we don't ping mid-work.
            if session_svc is not None:
                checker = getattr(session_svc, "is_turn_active", None)
                if callable(checker):
                    try:
                        if bool(checker(target_name)):
                            skipped_active_turn += 1
                            continue
                    except Exception:  # noqa: BLE001
                        pass

            # #296 — observable flow-state drift. Check BEFORE the
            # recent-event skip: a session that just fired a ``pm
            # notify`` has a very fresh event on the ledger (the
            # notify itself), which would otherwise suppress the
            # stuck_on_task path AND mask the drift. Drift detection
            # has its own dedupe via ``upsert_alert`` — the
            # ``state_drift:<task_id>`` alert type is unique per task
            # per open alert, so repeated sweeps don't spam.
            try:
                resolver = getattr(work, "_resolve_project_path", None)
                project_path = None
                if callable(resolver):
                    try:
                        project_path = resolver(task.project)
                    except Exception:  # noqa: BLE001
                        project_path = None
                if project_path is None:
                    project_path = services.project_root
                drift = reconcile_expected_advance(
                    task,
                    Path(project_path),
                    work,
                    state_store=state_store,
                    now=now,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: drift reconcile failed for %s",
                    task.task_id, exc_info=True,
                )
                drift = None
            if drift is not None:
                drift_detected += 1
                current_node = getattr(task, "current_node_id", "") or ""
                message = (
                    f"task {task.task_id}: observed "
                    f"{drift.advance_to_node} deliverables, advancing "
                    f"from {current_node} to {drift.advance_to_node} — "
                    f"{drift.reason}"
                )
                if state_store is not None:
                    # Event — permanent record of the drift detection.
                    # Keyed to the session so operators can scope.
                    try:
                        state_store.record_event(
                            target_name, "state_drift", message,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Alert — visible warning for Polly / the cockpit.
                    # Alert type carries the task id so drift on two
                    # different tasks surfaces as two distinct alerts.
                    alert_type = f"state_drift:{task.task_id}"
                    try:
                        # Detect whether the alert is new so our counter
                        # reflects real user-visible notifications.
                        existing = state_store.execute(
                            "SELECT id FROM alerts WHERE session_name = ? "
                            "AND alert_type = ? AND status = 'open'",
                            (target_name, alert_type),
                        ).fetchone()
                        state_store.upsert_alert(
                            target_name,
                            alert_type,
                            "warn",
                            (
                                f"{target_name} drift on {task.task_id}: "
                                f"{drift.reason}"
                            ),
                        )
                        if existing is None:
                            drift_alerted += 1
                    except Exception:  # noqa: BLE001
                        pass

            # Skip sessions that have recorded ANY event recently — the
            # session is clearly still doing something; a stale task
            # here is orthogonal to session liveness.
            if state_store is not None:
                try:
                    row = state_store.execute(
                        "SELECT created_at FROM events WHERE session_name = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (target_name,),
                    ).fetchone()
                    if row and row[0]:
                        last_ts = datetime.fromisoformat(row[0])
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=UTC)
                        if (now - last_ts) < timedelta(
                            seconds=STALE_THRESHOLD_SECONDS,
                        ):
                            skipped_recent_event += 1
                            continue
                except Exception:  # noqa: BLE001
                    pass

            # Fire the ping — notify() enforces the 30-min dedupe.
            try:
                outcome = _notify(
                    event,
                    services=services,
                    throttle_seconds=DEDUPE_WINDOW_SECONDS,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: notify failed for %s",
                    event.task_id, exc_info=True,
                )
                continue
            result = str(outcome.get("outcome", ""))
            if result == "deduped":
                deduped += 1
            elif result == "sent":
                pinged += 1
                # Raise a low-severity stuck_on_task alert so the
                # cockpit surfaces the event to the operator.
                if state_store is not None:
                    try:
                        state_store.upsert_alert(
                            target_name,
                            f"stuck_on_task:{event.task_id}",
                            "warning",
                            (
                                f"Session {target_name} stuck on "
                                f"{event.task_id} — resume ping sent"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        closer = getattr(work, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    return {
        "outcome": "swept",
        "considered": considered,
        "pinged": pinged,
        "deduped": deduped,
        "skipped_active_turn": skipped_active_turn,
        "skipped_recent_event": skipped_recent_event,
        "skipped_no_session": skipped_no_session,
        "drift_detected": drift_detected,
        "drift_alerted": drift_alerted,
    }


def alerts_gc_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Release expired leases and prune old events/heartbeat rows.

    Leases are auto-released via ``Supervisor.release_expired_leases``;
    stale rows are pruned via ``StateStore.prune_old_data``. Both are
    cheap, idempotent, and safe to run from any worker thread.
    """
    config, store = _load_config_and_store(payload)

    # Lease GC lives on the supervisor because it records events with
    # owner context; use a transient supervisor here.
    from pollypm.supervisor import Supervisor

    supervisor = Supervisor(config)
    released = supervisor.release_expired_leases()

    pruned = store.prune_old_data()
    return {
        "leases_released": len(released),
        "events_pruned": int(pruned.get("events", 0)),
        "heartbeats_pruned": int(pruned.get("heartbeats", 0)),
    }


def db_vacuum_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an incremental vacuum against StateStore to reclaim freelist pages.

    Incremental vacuum is a low-cost operation that only touches pages on
    the SQLite freelist — it does not rewrite the whole database the way
    a full ``VACUUM`` would. Safe to call daily. The shared StateStore
    connection coordinates with concurrent writers via busy_timeout, so
    no external lock is needed beyond what StateStore already holds.
    """
    _config, store = _load_config_and_store(payload)
    bytes_reclaimed = store.incremental_vacuum()
    mb_reclaimed = bytes_reclaimed / (1024 * 1024)
    store.record_event(
        session_name="system",
        event_type="db.vacuum",
        message=f"reclaimed {mb_reclaimed:.1f}MB",
    )
    return {"bytes_reclaimed": bytes_reclaimed, "mb_reclaimed": mb_reclaimed}


def memory_ttl_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop expired memory_entries (TTL in the past).

    Only affects rows with ``ttl_at IS NOT NULL``. Rows without an
    explicit TTL are left alone — retention policy for those is a
    separate decision, this handler just enforces what's already on
    the row.
    """
    _config, store = _load_config_and_store(payload)
    deleted = store.sweep_expired_memory_entries()
    store.record_event(
        session_name="system",
        event_type="memory.ttl_sweep",
        message=f"dropped {deleted} expired entries",
    )
    return {"deleted": deleted}


def agent_worktree_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Prune stale Claude Code harness agent worktrees under ``.claude/worktrees/``.

    These are NOT PollyPM task worktrees (those live under ``<project>/.pollypm/
    worktrees/...`` and are owned by ``teardown_worker``). They are harness
    worktrees spawned by background ``Agent()`` calls with
    ``isolation: "worktree"``. The harness doesn't always clean up after
    itself — on Sam's machine the directory bloated to 6.6 GB across 59
    worktrees. This handler performs conservative cleanup:

    * Merged-to-main branches: prune via ``git worktree remove --force`` and
      drop the local branch.
    * Unmerged + mtime > 7 days: log a warning but do not delete (may be
      in-progress uncommitted work).
    * mtime < 1 hour: skip (still actively in use).

    Only directories matching ``<repo_root>/.claude/worktrees/agent-*`` are
    considered. The repo root is taken from ``payload['project_root']`` if
    provided, otherwise from config's ``project.root_dir``.
    """
    import subprocess
    import time

    hint = payload.get("project_root") if isinstance(payload, dict) else None
    if hint:
        repo_root = Path(hint)
    else:
        config, _store = _load_config_and_store(payload)
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

    # Pre-compute the merged-branch set once via two cheap calls — cheaper
    # than spawning two git processes per worktree.
    merged_local = _git(repo_root, "branch", "--merged", "main")
    merged_remote = _git(repo_root, "branch", "-r", "--merged", "origin/main")
    merged_names: set[str] = set()
    for proc in (merged_local, merged_remote):
        if proc.returncode != 0:
            continue
        for raw in proc.stdout.splitlines():
            # ``git branch`` prefixes lines with ``* `` (current),
            # ``+ `` (checked out in another worktree), or spaces.
            name = raw.strip().lstrip("*+").strip()
            if not name or name.startswith("("):
                continue
            # Normalize ``origin/foo`` → ``foo`` so local+remote merge into
            # one name set.
            if name.startswith("origin/"):
                name = name[len("origin/"):]
            merged_names.add(name)

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

            if branch in merged_names:
                remove_proc = _git(
                    repo_root, "worktree", "remove", "--force", str(wt),
                )
                if remove_proc.returncode != 0:
                    errors += 1
                    continue
                # Best-effort local branch delete — don't fail the prune
                # if the branch was already gone.
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

    # Clean up any dangling worktree admin entries (e.g. directories that
    # were removed on disk but still registered in ``.git/worktrees``).
    _git(repo_root, "worktree", "prune")

    return {
        "pruned": pruned,
        "skipped_active": skipped_active,
        "warned_stale": warned_stale,
        "errors": errors,
    }


def log_rotate_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Rotate + prune oversized log files under ``config.project.logs_dir``.

    Unbounded tmux ``pipe-pane`` captures previously let individual logs
    grow to tens of megabytes (``pm-operator.log`` hit 60 MB on Sam's
    machine, total ``~/.pollypm/logs/`` reached 745 MB). This handler
    implements rename-then-truncate rotation + gzip of the rotated
    archive + retention pruning of older ``.log.<ts>.gz`` siblings.

    Algorithm per ``<logs_dir>/*.log`` file:

    1. If size <= ``rotate_size_mb`` MB → skip.
    2. Else: rename ``<name>.log`` → ``<name>.log.<ts>`` (atomic on
       POSIX). Active writers keep their open file descriptor pointed
       at the renamed inode — they do not follow the rename — so we
       recreate an empty ``<name>.log`` so new appends (including
       ``tmux pipe-pane`` when it reopens) have somewhere to go. The
       original writers will continue writing to the now-renamed file
       until they close/reopen; we accept that small tail because tmux
       reopens on its own schedule.
    3. Gzip-in-place: ``<name>.log.<ts>`` → ``<name>.log.<ts>.gz``.
    4. Retention: keep only the ``rotate_keep`` most recent ``.log.*.gz``
       siblings per base name; delete older rotations.

    Non-``.log`` files in the directory (e.g. JSON state blobs) are
    never touched. A missing ``logs_dir`` is a no-op — returns zeros
    with no error.

    Payload overrides (for tests + ad-hoc runs):
    * ``logs_dir`` — override ``config.project.logs_dir``.
    * ``rotate_size_mb`` — override ``config.logging.rotate_size_mb``.
    * ``rotate_keep`` — override ``config.logging.rotate_keep``.
    """
    import gzip
    import os
    import re
    import shutil
    import time

    # Resolve logs_dir + thresholds. When a ``logs_dir`` override is
    # present in the payload we skip loading config entirely so tests
    # don't need a full PollyPM config on disk.
    logs_dir_hint = payload.get("logs_dir") if isinstance(payload, dict) else None
    size_override = payload.get("rotate_size_mb") if isinstance(payload, dict) else None
    keep_override = payload.get("rotate_keep") if isinstance(payload, dict) else None

    if logs_dir_hint is not None:
        logs_dir = Path(logs_dir_hint)
        rotate_size_mb = int(size_override) if size_override is not None else 20
        rotate_keep = int(keep_override) if keep_override is not None else 3
    else:
        config, _store = _load_config_and_store(payload)
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

    # Fixed ts-suffix pattern: <base>.log.<digits>.gz — we use epoch
    # seconds so retention ordering is a simple numeric sort.
    rotation_re = re.compile(r"^(?P<base>.+)\.log\.(?P<ts>\d+)\.gz$")

    for log_path in sorted(logs_dir.glob("*.log")):
        if not log_path.is_file():
            continue
        try:
            size = log_path.stat().st_size
        except OSError:
            errors += 1
            continue
        if size <= threshold_bytes:
            continue
        # Rotate. Use epoch seconds for the stamp — it sorts numerically
        # and avoids the filesystem-safety concerns of ISO strings.
        ts = int(time.time())
        rotated_path = log_path.with_suffix(f".log.{ts}")
        # If the rotated name already exists (two rotations in the same
        # second), bump until free.
        bump = 0
        while rotated_path.exists():
            bump += 1
            rotated_path = log_path.with_suffix(f".log.{ts}.{bump}")
        try:
            # Atomic rename on POSIX. Writers with the file open keep
            # their fd; new opens see the fresh empty file we create
            # next.
            os.rename(log_path, rotated_path)
            # Recreate an empty <name>.log so the next writer to open()
            # finds it. ``touch`` semantics.
            log_path.touch()
        except OSError:
            logger.debug(
                "log.rotate: rename failed for %s", log_path, exc_info=True,
            )
            errors += 1
            continue
        # Gzip the rotated file in place. Stream to avoid loading the
        # whole thing into memory.
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
            # Leave the uncompressed rotation in place so it's not lost.
            continue

    # Retention pass: group ``<base>.log.<ts>.gz`` files by base name
    # and delete all but the newest ``rotate_keep``.
    by_base: dict[str, list[tuple[int, Path]]] = {}
    for gz in logs_dir.glob("*.log.*.gz"):
        m = rotation_re.match(gz.name)
        if not m:
            continue
        try:
            ts_val = int(m.group("ts"))
        except ValueError:
            continue
        by_base.setdefault(m.group("base"), []).append((ts_val, gz))

    for base, entries in by_base.items():
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


def notification_staging_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop flushed + silent notification_staging rows older than 30d.

    Pending digest rows are never pruned — they belong to a milestone
    that simply hasn't closed yet. Opens a short-lived work-service
    connection so the staging table is guaranteed to exist (the
    SQLiteWorkService init path runs the migration).
    """
    import sqlite3

    from pollypm.notification_staging import prune_old_staging

    _config, store = _load_config_and_store(payload)
    retain_days = int(payload.get("retain_days") or 30)

    # The staging table lives in the shared state.db alongside work_*
    # tables; the work-service migration (v4) creates it. We open a
    # direct connection here because the prune is a pure DML op and
    # does not need the full service wrapper.
    db_path = getattr(store, "path", None) or _config.project.state_db
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # Make sure the schema is present — safe no-op when the table
        # already exists (e.g. SQLiteWorkService ran migration v4).
        from pollypm.work.schema import create_work_tables
        create_work_tables(conn)
        summary = prune_old_staging(conn, retain_days=retain_days)
    finally:
        conn.close()

    store.record_event(
        session_name="system",
        event_type="notification_staging.prune",
        message=(
            f"pruned {summary['flushed_pruned']} flushed + "
            f"{summary['silent_pruned']} silent rows (>{retain_days}d)"
        ),
    )
    return summary


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "session.health_sweep", session_health_sweep_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "capacity.probe", capacity_probe_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "transcript.ingest", transcript_ingest_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "alerts.gc", alerts_gc_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "work.progress_sweep", work_progress_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # DB hygiene — incremental vacuum + memory TTL sweep. Both are cheap
    # daily sweeps that coordinate with the shared StateStore connection.
    api.register_handler(
        "db.vacuum", db_vacuum_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "memory.ttl_sweep", memory_ttl_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "notification_staging.prune", notification_staging_prune_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # Harness agent-worktree hygiene — hourly prune of merged/stale
    # worktrees under ``<repo_root>/.claude/worktrees/agent-*``.
    api.register_handler(
        "agent_worktree.prune", agent_worktree_prune_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    # Log-file hygiene — hourly rotation + gzip of oversized logs.
    api.register_handler(
        "log.rotate", log_rotate_handler,
        max_attempts=1, timeout_seconds=120.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # Cadences match the task spec for issue #164. ``inbox.sweep`` was
    # removed with the legacy inbox subsystem (see iv04).
    api.register_recurring("@every 10s", "session.health_sweep", {})
    api.register_recurring("@every 60s", "capacity.probe", {})
    api.register_recurring("@every 30s", "transcript.ingest", {})
    api.register_recurring("@every 5m", "alerts.gc", {})
    # #249 — work-service-aware stuck-task sweeper.
    api.register_recurring("@every 5m", "work.progress_sweep", {})
    # DB hygiene — daily around 4am local. Off-minute (``7``) avoids
    # fleet-wide sync if many cockpits run on the same host. Memory TTL
    # sweep runs a few minutes later so its writes don't collide with
    # the vacuum's page-reclaim pass.
    api.register_recurring("7 4 * * *", "db.vacuum", {}, dedupe_key="db.vacuum")
    api.register_recurring(
        "13 4 * * *", "memory.ttl_sweep", {}, dedupe_key="memory.ttl_sweep",
    )
    # Notification staging hygiene — flushed rollup rows and silent audit
    # rows older than 30 days are dropped. Pending digest rows are left
    # alone (they belong to a milestone that hasn't closed yet).
    api.register_recurring(
        "19 4 * * *", "notification_staging.prune", {},
        dedupe_key="notification_staging.prune",
    )
    # Harness agent-worktree hygiene — every hour at minute 23, off-pattern
    # from the 4am DB hygiene window. Only prunes merged branches; stale
    # unmerged trees are logged but left intact.
    api.register_recurring(
        "23 * * * *", "agent_worktree.prune", {},
        dedupe_key="agent_worktree.prune",
    )
    # Log-file hygiene — every hour at minute 31, off-pattern from the
    # agent-worktree prune (:23) and the 4am DB hygiene window. Rotates
    # any ``<logs_dir>/*.log`` over the configured threshold and keeps
    # only the most recent N gzipped rotations.
    api.register_recurring(
        "31 * * * *", "log.rotate", {},
        dedupe_key="log.rotate",
    )


plugin = PollyPMPlugin(
    name="core_recurring",
    version="0.1.0",
    description=(
        "Built-in recurring handlers — migrated from the old heartbeat loop. "
        "Registers inbox sweep, session health sweep, capacity probe, "
        "transcript ingest, alerts GC, and work-service progress sweep on "
        "the roster + job queue."
    ),
    capabilities=(
        Capability(kind="job_handler", name="inbox.sweep"),
        Capability(kind="job_handler", name="session.health_sweep"),
        Capability(kind="job_handler", name="capacity.probe"),
        Capability(kind="job_handler", name="transcript.ingest"),
        Capability(kind="job_handler", name="alerts.gc"),
        Capability(kind="job_handler", name="work.progress_sweep"),
        Capability(kind="job_handler", name="db.vacuum"),
        Capability(kind="job_handler", name="memory.ttl_sweep"),
        Capability(kind="job_handler", name="notification_staging.prune"),
        Capability(kind="job_handler", name="agent_worktree.prune"),
        Capability(kind="job_handler", name="log.rotate"),
        Capability(kind="roster_entry", name="core_recurring"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
