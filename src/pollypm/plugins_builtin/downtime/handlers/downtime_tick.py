"""Downtime roster tick.

Registered as ``downtime.tick`` on the job-handler registry, fired every
12h by the roster (configurable via ``[downtime].cadence``). The handler
is cheap: it walks a chain of short-circuit gates and either schedules
one exploration task or returns a structured skip reason.

Gate order (first-match-wins):

1. Config missing → ``no-config``.
2. ``[downtime].enabled = false`` → ``disabled``.
3. ``pause_until`` in the future → ``paused``.
4. Capacity used% ≥ threshold → ``capacity-too-high``.
5. An active downtime task already exists → ``throttled``.
6. No candidate available → ``no-candidates``.

Otherwise, the handler calls ``pick_candidate(project)``, creates a
work-service task on the ``downtime_explore`` flow, and returns the
scheduled task id.

The actual candidate selection + work-service call are module-level
callables so tests can swap them in without touching the tick logic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from pollypm.plugins_builtin.downtime.settings import (
    DowntimeSettings,
    load_downtime_settings,
)
from pollypm.plugins_builtin.downtime.state import (
    DowntimeState,
    load_state,
    save_state,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate + candidate sourcing — dt03 ships the real implementation; we
# re-export from pick_candidate so existing tests and downstream handlers
# that imported ``Candidate`` from this module keep working.
# ---------------------------------------------------------------------------


from pollypm.plugins_builtin.downtime.handlers import pick_candidate as _pick_candidate


Candidate = _pick_candidate.Candidate


def pick_candidate(
    *,
    config: Any,
    settings: DowntimeSettings,
    state: DowntimeState,
    project_root: Path,
) -> Candidate | None:
    """Delegate to dt03's real candidate sourcer.

    Kept as a thin wrapper so tests can monkeypatch
    ``downtime_tick.pick_candidate`` without reaching into the sourcing
    module. This preserves the dt01 test contract while dt03 lands the
    actual implementation.
    """
    return _pick_candidate.pick_candidate(
        config=config,
        settings=settings,
        state=state,
        project_root=project_root,
    )


def schedule_downtime_task(
    *,
    candidate: Candidate,
    project: str,
    config: Any,
    db_path: Path | None = None,
) -> str | None:
    """Create a work-service task for the given candidate.

    Returns the task id (``"project/N"``) on success, or ``None`` if the
    work service is unavailable. The task uses the ``downtime_explore``
    flow, priority ``low``, label ``downtime``, and surfaces the
    candidate kind as an additional label so later filters can pick
    matching handlers without reparsing the description.
    """
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001 - work service optional in some tests
        logger.warning("downtime: work service unavailable, cannot schedule task")
        return None

    try:
        svc = SQLiteWorkService(
            db_path=str(db_path) if db_path else ":memory:",
            project_path=project,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("downtime: failed to open work service: %s", exc)
        return None

    labels = ["downtime", f"kind:{candidate.kind}", f"source:{candidate.source}"]
    try:
        task = svc.create(
            title=candidate.title,
            description=candidate.description,
            type="task",
            project=project,
            flow_template="downtime_explore",
            roles={"explorer": "worker"},
            priority="low",
            labels=labels,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("downtime: failed to create task: %s", exc)
        return None
    return task.task_id


# ---------------------------------------------------------------------------
# Pure gate helpers
# ---------------------------------------------------------------------------


def is_paused(state: DowntimeState, *, now: datetime) -> bool:
    """True when ``pause_until`` is present and strictly in the future."""
    if not state.pause_until:
        return False
    raw = state.pause_until.strip()
    if not raw:
        return False
    # Accept pure date (YYYY-MM-DD) as "end of that day" — pause through the
    # day. Accept full ISO-8601 as-is.
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw + "T23:59:59")
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt > now


def compute_used_pct(config: Any, store: Any) -> int | None:
    """Return the currently-used-capacity percentage for the primary account.

    "Primary" = the configured controller account if present, else the
    minimum remaining across all accounts (worst-case view). Returns
    ``None`` if no usage data is available anywhere; callers treat
    ``None`` as "unknown — allow the tick to proceed", matching the
    spec's conservative-but-permissive stance (we don't want a probe
    failure to silently disable downtime forever).
    """
    try:
        from pollypm.capacity import probe_all_accounts
    except Exception:  # noqa: BLE001
        return None
    if config is None or store is None:
        return None
    try:
        probes = probe_all_accounts(config, store)
    except Exception as exc:  # noqa: BLE001
        logger.debug("downtime: probe_all_accounts failed: %s", exc)
        return None

    remainings: list[int] = []
    controller = getattr(getattr(config, "pollypm", None), "controller_account", None)
    controller_remaining: int | None = None

    for probe in probes:
        rem = getattr(probe, "remaining_pct", None)
        if rem is None:
            continue
        remainings.append(int(rem))
        if controller and probe.account_name == controller:
            controller_remaining = int(rem)

    if controller_remaining is not None:
        return max(0, min(100, 100 - controller_remaining))
    if remainings:
        # Worst-case view: the account with the highest used% is the
        # bottleneck. used% = 100 - remaining%.
        used_worst = 100 - min(remainings)
        return max(0, min(100, used_worst))
    return None


def has_active_downtime_task(
    *,
    project: str,
    db_path: Path | None = None,
) -> bool:
    """Return True if any downtime-labelled task is ``in_progress`` or ``review``."""
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        return False
    if db_path is None:
        return False
    try:
        svc = SQLiteWorkService(db_path=str(db_path), project_path=project)
    except Exception:
        return False
    try:
        in_progress = svc.list_tasks(project=project, work_status="in_progress")
        review = svc.list_tasks(project=project, work_status="review")
    except Exception as exc:  # noqa: BLE001
        logger.debug("downtime: list_tasks failed: %s", exc)
        return False
    candidates = list(in_progress) + list(review)
    for task in candidates:
        labels = getattr(task, "labels", None) or []
        if "downtime" in labels:
            return True
    return False


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def downtime_tick_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Entrypoint registered as ``downtime.tick``.

    Payload keys (all optional):

    * ``config_path`` — explicit pollypm.toml to load. Defaults to global
      discovery.
    * ``project`` — project key override. Defaults to the config's root
      project name.
    * ``now`` — ISO-8601 override for tests.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    config_path_override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = (
        Path(config_path_override) if config_path_override else resolve_config_path(DEFAULT_CONFIG_PATH)
    )
    if not config_path.exists():
        return {"scheduled": None, "skipped": True, "reason": "no-config", "config_path": str(config_path)}

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("downtime: failed to load config %s: %s", config_path, exc)
        return {"scheduled": None, "skipped": True, "reason": "config-error", "error": str(exc)}

    settings = load_downtime_settings(config_path)

    now_raw = payload.get("now") if isinstance(payload, dict) else None
    if isinstance(now_raw, datetime):
        now = now_raw if now_raw.tzinfo else now_raw.replace(tzinfo=UTC)
    elif isinstance(now_raw, str) and now_raw:
        try:
            now = datetime.fromisoformat(now_raw)
        except ValueError:
            now = datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    else:
        now = datetime.now(UTC)

    project = payload.get("project") if isinstance(payload, dict) else None
    if not project:
        project = getattr(getattr(config, "project", None), "name", "") or "default"

    base_dir = getattr(config.project, "base_dir", None)
    if base_dir is None:
        return {"scheduled": None, "skipped": True, "reason": "no-base-dir"}
    base_dir = Path(base_dir)

    state = load_state(base_dir)

    # 1. Enabled?
    if not settings.enabled:
        return {"scheduled": None, "skipped": True, "reason": "disabled"}

    # 2. Paused?
    if is_paused(state, now=now):
        return {
            "scheduled": None,
            "skipped": True,
            "reason": "paused",
            "pause_until": state.pause_until,
        }

    # 3. Capacity.
    store = None
    try:
        from pollypm.storage.state import StateStore

        state_db = getattr(config.project, "state_db", None)
        if state_db is not None:
            store = StateStore(Path(state_db))
    except Exception as exc:  # noqa: BLE001
        logger.debug("downtime: state store unavailable: %s", exc)

    used_pct = compute_used_pct(config, store)
    if used_pct is not None and used_pct >= settings.threshold_pct:
        return {
            "scheduled": None,
            "skipped": True,
            "reason": "capacity-too-high",
            "used_pct": used_pct,
            "threshold_pct": settings.threshold_pct,
        }

    # 4. Already in progress?
    state_db_path = Path(config.project.state_db) if getattr(config.project, "state_db", None) else None
    work_db_path = base_dir / "work.db"
    if work_db_path.exists() and has_active_downtime_task(project=project, db_path=work_db_path):
        return {"scheduled": None, "skipped": True, "reason": "throttled"}

    # 5. Pick a candidate.
    # Indirect through the module so tests can monkeypatch ``pick_candidate``.
    from pollypm.plugins_builtin.downtime.handlers import downtime_tick as _self

    project_root = Path(getattr(config.project, "root_dir", Path.cwd()))
    candidate = _self.pick_candidate(
        config=config, settings=settings, state=state, project_root=project_root,
    )
    if candidate is None:
        return {"scheduled": None, "skipped": True, "reason": "no-candidates"}

    # 5a. Respect disabled_categories. Belt-and-suspenders: dt03's
    # pick_candidate should already filter, but the tick handler is the
    # last line of defense against a stale candidate cache.
    if candidate.kind in settings.disabled_categories:
        return {
            "scheduled": None,
            "skipped": True,
            "reason": "category-disabled",
            "kind": candidate.kind,
        }

    # 6. Schedule it.
    task_id = _self.schedule_downtime_task(
        candidate=candidate,
        project=project,
        config=config,
        db_path=work_db_path if work_db_path.exists() else None,
    )
    if task_id is None:
        return {
            "scheduled": None,
            "skipped": True,
            "reason": "schedule-failed",
            "kind": candidate.kind,
        }

    # Record the selection in state so the next tick can prefer variety.
    state.note_scheduled(kind=candidate.kind, source=candidate.source, title=candidate.title)
    try:
        save_state(base_dir, state)
    except Exception as exc:  # noqa: BLE001
        logger.debug("downtime: save_state failed: %s", exc)

    return {
        "scheduled": task_id,
        "skipped": False,
        "reason": "ok",
        "kind": candidate.kind,
        "source": candidate.source,
        "title": candidate.title,
    }
