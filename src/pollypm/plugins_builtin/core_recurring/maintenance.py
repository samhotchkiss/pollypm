"""Maintenance-style recurring handlers for core_recurring."""

from __future__ import annotations

import logging
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

    account_names = payload.get("accounts")
    if not isinstance(account_names, list):
        account_names = None
    samples = refresh_all_account_usage(
        _resolve_config_path(payload),
        account_names=account_names,
    )
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
    }


def transcript_ingest_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tail provider transcripts into the shared events ledger."""
    config = _load_config(payload)

    from pollypm.transcript_ingest import sync_transcripts_once

    sync_transcripts_once(config)
    return {"ok": True}


def db_vacuum_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an incremental vacuum against StateStore to reclaim freelist pages."""
    with _load_config_and_store(payload) as (_config, store):
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
        return {}
    try:
        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        # The cached host is keyed on the project root_dir. Use the
        # config's parent as a stable key so a per-project sweep finds
        # the same registry as boot.
        host = extension_host_for_root(str(config_path.parent))
        registry = host.job_handler_registry()
    except Exception:  # noqa: BLE001
        return {}
    try:
        snapshot = registry.snapshot()
    except Exception:  # noqa: BLE001
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
                from sqlalchemy import and_ as _and
                from sqlalchemy import delete as _delete

                from pollypm.store.schema import messages as _messages

                result = msg_store.execute(
                    _delete(_messages).where(
                        _and(
                            _messages.c.type == "event",
                            _messages.c.subject.notin_(tuple(known)),
                            _messages.c.created_at < default_cutoff,
                        )
                    )
                )
                deleted_default = int(getattr(result, "rowcount", 0) or 0)
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
    """Delete ``type='event'`` rows matching ``subject`` older than ``cutoff``."""
    try:
        from sqlalchemy import and_ as _and
        from sqlalchemy import delete as _delete

        from pollypm.store.schema import messages as _messages

        result = msg_store.execute(
            _delete(_messages).where(
                _and(
                    _messages.c.type == "event",
                    _messages.c.subject == subject,
                    _messages.c.created_at < cutoff,
                )
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception:  # noqa: BLE001
        logger.debug(
            "events.retention_sweep: delete failed for subject=%s",
            subject, exc_info=True,
        )
        return 0


def memory_ttl_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop expired memory_entries (TTL in the past)."""
    with _load_config_and_store(payload) as (_config, store):
        deleted = store.sweep_expired_memory_entries()
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


def agent_worktree_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Prune stale Claude Code harness agent worktrees under ``.claude/worktrees/``."""
    import subprocess
    import time

    hint = payload.get("project_root") if isinstance(payload, dict) else None
    if hint:
        repo_root = Path(hint)
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

    for _base, entries in by_base.items():
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
    """Drop flushed + silent notification_staging rows older than 30d."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    with _load_config_and_store(payload) as (_config, store):
        retain_days = int(payload.get("retain_days") or 30)
        db_path = getattr(store, "path", None) or _config.project.state_db
        with SQLiteWorkService(db_path=db_path) as svc:
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
