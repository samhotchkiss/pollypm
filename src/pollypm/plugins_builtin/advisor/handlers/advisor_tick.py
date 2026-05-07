"""Half-hourly advisor tick.

Registered as ``advisor.tick`` on the job-handler registry and fired by
the roster on the configured cadence (default ``@every 30m``). The
handler itself is cheap: it walks tracked projects, checks the enabled
flag + pause marker, calls ``detect_changes`` (stubbed in ad01, real in
ad02), and for projects with meaningful activity enqueues an
``advisor_review`` work task — unless an earlier advisor task is still
in flight, in which case the project is skipped to prevent pile-up.

Out of scope for ad01:

* Real change detection (ad02 replaces the stub in ``detect_changes.py``).
* Context packing + session launch (ad03).
* History log write (ad04).
* Inbox emission on emit (ad05).

The tick handler is intentionally non-raising — any per-project failure
is logged and the next project still gets its turn.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.advisor.settings import (
    AdvisorSettings,
    load_advisor_settings,
)
from pollypm.plugins_builtin.advisor.state import (
    ProjectAdvisorState,
    is_paused,
    iso_utc_now,
    load_state,
    save_state,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage-layout resolution (#1037)
# ---------------------------------------------------------------------------


def _resolve_state_db(project_path: Path) -> Path | None:
    """Return the on-disk ``state.db`` that backs ``project_path``, or None.

    Mirrors the dual-path canonical pattern in
    :func:`pollypm.cockpit_inbox._inbox_db_sources`: per-project
    ``<project_path>/.pollypm/state.db`` first (legacy / per-project
    layout), then walk up the parents looking for a workspace-root
    ``<ancestor>/.pollypm/state.db`` (the layout #339 collapsed everything
    onto). Returns ``None`` when neither exists.

    Used by :func:`has_project_stagnation_candidate`,
    :func:`enqueue_advisor_review`, and ``detect_changes._transitions_db_path``
    so a tracked project that lives under a workspace-root state.db
    (every install post-#339) is still discoverable. Without this the
    advisor probe returns False on every tick and the user sees
    ``last run: (never)`` forever — see #1037.
    """
    from pollypm.plugins_builtin.advisor.db_paths import resolve_state_db

    return resolve_state_db(project_path)


# ---------------------------------------------------------------------------
# Stubs for downstream issues. ad02 replaces detect_changes; ad03 replaces
# run_advisor_session. Wired through a module-level callable so tests can
# monkeypatch them independently.
# ---------------------------------------------------------------------------


def detect_changes(
    project_path: Path,
    since: datetime | None,
    *,
    project_key: str | None = None,
    work_service: Any | None = None,
) -> bool:
    """Return True when the project has a commit or task transition since ``since``.

    Thin adapter over :func:`pollypm.plugins_builtin.advisor.handlers.
    detect_changes.detect_changes` — unit tests of the tick handler
    monkeypatch this symbol to force particular outcomes; ad03's
    context-packing imports the full :class:`ChangeReport` directly
    from ``detect_changes.py``.
    """
    from pollypm.plugins_builtin.advisor.handlers.detect_changes import (
        detect_changes as _detect,
    )
    report = _detect(
        project_path,
        since,
        project_key=project_key,
        work_service=work_service,
    )
    return report.has_changes


def enqueue_advisor_review(
    *,
    project_key: str,
    project_path: Path,
    config: Any,
    work_service: Any | None = None,
) -> dict[str, Any]:
    """Create an advisor_review work-service task for a project.

    Kept as a module-level callable so unit tests can monkeypatch the
    enqueue path without touching the work service.
    """
    close_service = False
    if work_service is None:
        from pollypm.work import create_work_service

        # Prefer an existing state.db (per-project or workspace-root,
        # #1037). Only synthesize the per-project path when neither
        # exists — matches the pre-#339 fallback so first-run installs
        # still get a writable target.
        db_path = _resolve_state_db(project_path)
        if db_path is None:
            db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        work_service = create_work_service(db_path=db_path, project_path=project_path)
        close_service = True

    try:
        task = work_service.create(
            title=f"Advisor review for {project_key}",
            description=(
                "Review recent project trajectory, identify stalls or human "
                "blockers, and emit a structured advisor decision."
            ),
            type="task",
            project=project_key,
            flow_template="advisor_review",
            roles={"advisor": "advisor"},
            priority="normal",
            created_by="advisor.tick",
            labels=["advisor"],
            requires_human_review=False,
        )
        queued = work_service.queue(task.task_id, "advisor.tick")
        return {
            "enqueued": True,
            "project": project_key,
            "task_id": queued.task_id,
        }
    finally:
        if close_service:
            close = getattr(work_service, "close", None)
            if callable(close):
                close()


def has_in_progress_advisor_task(
    *,
    project_key: str,
    work_service: Any,
) -> bool:
    """Return True if any advisor task is currently active for the project.

    An advisor task is "active" when its ``work_status`` is one of
    ``in_progress`` / ``queued`` / ``review`` *and* its labels list
    contains ``"advisor"``. This is the pile-up throttle: if a session
    is still running from a previous tick we skip instead of stacking
    work.

    Non-raising. A work-service lookup failure is logged and treated
    as "no advisor task active" so the tick can proceed.
    """
    if work_service is None:
        return False
    try:
        # Ask for everything non-terminal on the project, then filter
        # by the advisor label client-side — the service protocol
        # doesn't expose label filtering, and projects have few tasks
        # at any given time so the cost is negligible.
        candidates = []
        for status in ("in_progress", "queued", "review"):
            try:
                tasks = work_service.list_tasks(
                    project=project_key,
                    work_status=status,
                )
            except Exception:  # noqa: BLE001
                tasks = []
            for t in tasks:
                labels = getattr(t, "labels", []) or []
                if "advisor" in labels:
                    candidates.append(t)
        return bool(candidates)
    except Exception as exc:  # noqa: BLE001
        logger.debug("advisor: throttle lookup failed for %s: %s", project_key, exc)
        return False


def has_project_stagnation_candidate(
    *,
    project_key: str,
    project_path: Path,
    work_service: Any,
) -> bool:
    """Return True when a quiet project still has non-terminal work.

    This is intentionally broad. The advisor LLM decides whether the
    situation is normal waiting, user-blocked, dependency-blocked, or
    actually stuck; deterministic code only decides whether the state is
    worth re-evaluating.
    """
    close_service = False
    if work_service is None:
        db_path = _resolve_state_db(project_path)
        if db_path is None:
            return False
        try:
            from pollypm.work import create_work_service
            work_service = create_work_service(db_path=db_path, project_path=project_path)
            close_service = True
        except Exception:  # noqa: BLE001
            return False
    try:
        tasks = work_service.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if close_service:
            close = getattr(work_service, "close", None)
            if callable(close):
                close()

    for task in tasks:
        status = getattr(getattr(task, "work_status", None), "value", None) or getattr(task, "work_status", "")
        if status not in {"done", "cancelled"}:
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iter_tracked_projects(config: Any) -> list[tuple[str, Path]]:
    """Return ``(project_key, project_path)`` for each tracked project.

    Tracked means one of:

    * ``config.projects[key].tracked == True``, OR
    * The ambient project (``config.project.root_dir``), if present —
      single-project installs without explicit ``[projects.*]`` entries
      still get advisor coverage.

    Entries whose on-disk path doesn't exist are filtered out (a stale
    config should not crash the tick).
    """
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    projects = getattr(config, "projects", {}) or {}
    for key, known in projects.items():
        if not getattr(known, "tracked", False):
            continue
        path = getattr(known, "path", None)
        if path is None or not Path(path).exists():
            continue
        resolved = Path(path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append((key, Path(path)))

    ambient = getattr(getattr(config, "project", None), "root_dir", None)
    if ambient is not None:
        ambient_path = Path(ambient)
        if ambient_path.exists():
            resolved = ambient_path.resolve()
            if resolved not in seen:
                key = getattr(getattr(config, "project", None), "name", None) or "pollypm"
                out.append((str(key), ambient_path))

    return out


# ---------------------------------------------------------------------------
# Per-project decision
# ---------------------------------------------------------------------------


def _should_review(
    *,
    project_key: str,
    project_path: Path,
    project_state: ProjectAdvisorState,
    settings: AdvisorSettings,
    now_utc: datetime,
    work_service: Any,
) -> tuple[bool, str]:
    """Return (review?, reason). Reason is a short machine-parseable tag."""
    if not settings.enabled:
        return False, "plugin-disabled"
    if not project_state.enabled:
        return False, "project-disabled"
    if is_paused(project_state, now_utc=now_utc):
        return False, "paused"

    since = _parse_iso(project_state.last_run)

    try:
        # Call via module attribute so tests can monkeypatch detect_changes.
        from pollypm.plugins_builtin.advisor.handlers import advisor_tick as _self
        # detect_changes takes project_key + work_service kwargs in the ad02
        # signature; older tests that monkeypatch it with a simpler
        # positional-only signature still work because we route through
        # the adapter below.
        try:
            changed = bool(
                _self.detect_changes(
                    project_path, since,
                    project_key=project_key,
                    work_service=work_service,
                )
            )
        except TypeError:
            # Backwards-compat: some tests patch a two-arg stub.
            changed = bool(_self.detect_changes(project_path, since))
    except Exception as exc:  # noqa: BLE001
        logger.debug("advisor: detect_changes failed for %s: %s", project_key, exc)
        return False, "detect-error"

    if not changed:
        if not has_project_stagnation_candidate(
            project_key=project_key,
            project_path=project_path,
            work_service=work_service,
        ):
            return False, "no-changes"
        reason = "stagnation-candidate"
    else:
        reason = "ok"

    if has_in_progress_advisor_task(project_key=project_key, work_service=work_service):
        return False, "in-progress"

    return True, reason


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def advisor_tick_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Half-hourly advisor tick.

    Payload keys (all optional):

    * ``config_path`` — explicit ``pollypm.toml``.
    * ``now_utc`` — ISO-8601 UTC override (tests only).
    * ``work_service`` — explicit work-service handle (tests only).
      Production lookup uses the default factory.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    config_path_override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = (
        Path(config_path_override) if config_path_override
        else resolve_config_path(DEFAULT_CONFIG_PATH)
    )
    if not config_path.exists():
        return {
            "fired": False,
            "reason": "no-config",
            "config_path": str(config_path),
        }

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisor: failed to load config %s: %s", config_path, exc)
        return {"fired": False, "reason": "config-error", "error": str(exc)}

    settings = load_advisor_settings(config_path)

    if not settings.enabled:
        return {"fired": False, "reason": "plugin-disabled"}

    override_now = payload.get("now_utc") if isinstance(payload, dict) else None
    if isinstance(override_now, datetime):
        now_utc = override_now if override_now.tzinfo else override_now.replace(tzinfo=UTC)
    elif isinstance(override_now, str) and override_now:
        parsed = _parse_iso(override_now)
        now_utc = parsed or datetime.now(UTC)
    else:
        now_utc = datetime.now(UTC)

    base_dir: Path = Path(config.project.base_dir)

    # Explicit override so tests can inject a fake work service. Production
    # callers hand in ``None`` and the enqueue stub silently no-ops — the
    # real enqueue path lands in ad03.
    work_service = payload.get("work_service") if isinstance(payload, dict) else None

    state = load_state(base_dir)

    # Drop the per-tick change-detection cache so each tick starts fresh.
    # Cache is in-process only; it exists to deduplicate the work when
    # assess.py (ad03) re-reads a ChangeReport inside the same tick.
    try:
        from pollypm.plugins_builtin.advisor.handlers.detect_changes import clear_cache
        clear_cache()
    except Exception:  # noqa: BLE001
        pass

    projects = _iter_tracked_projects(config)
    results: list[dict[str, Any]] = []
    enqueued_projects: list[str] = []

    for project_key, project_path in projects:
        proj_state = state.get(project_key)
        proj_state.last_tick_at = now_utc.isoformat()

        review, reason = _should_review(
            project_key=project_key,
            project_path=project_path,
            project_state=proj_state,
            settings=settings,
            now_utc=now_utc,
            work_service=work_service,
        )

        entry: dict[str, Any] = {
            "project": project_key,
            "scheduled": False,
            "reason": reason,
        }

        if review:
            try:
                from pollypm.plugins_builtin.advisor.handlers import advisor_tick as _self
                outcome = _self.enqueue_advisor_review(
                    project_key=project_key,
                    project_path=project_path,
                    config=config,
                    work_service=work_service,
                )
                enqueued = bool(outcome.get("enqueued")) if isinstance(outcome, dict) else False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "advisor: enqueue failed for %s: %s", project_key, exc,
                )
                enqueued = False
                outcome = {"enqueued": False, "error": str(exc)}
            entry["scheduled"] = enqueued
            entry["reason"] = reason if enqueued else entry["reason"]
            entry["enqueue"] = outcome
            if enqueued:
                enqueued_projects.append(project_key)

        if reason == "stagnation-candidate":
            try:
                from pollypm.project_status_summary import (
                    compute_project_monitor_summary,
                    record_project_monitor_summary,
                )
                from pollypm.store import SQLAlchemyStore

                state_db = getattr(getattr(config, "project", None), "state_db", None)
                if state_db is not None:
                    store = SQLAlchemyStore(f"sqlite:///{Path(state_db)}")
                    try:
                        # #782: record a full monitor summary, not
                        # just a stalled-tasks placeholder. The helper
                        # walks the work service and fills
                        # completions, stalls, blockers, and the
                        # zero-completion flag so durable activity
                        # rows carry the full picture.
                        summary = compute_project_monitor_summary(
                            work_service=work_service,
                            project_key=project_key,
                        )
                        summary.automatic_next_actions = [
                            "advisor review queued for stagnation classification"
                            if entry.get("scheduled")
                            else "advisor review considered but not queued"
                        ]
                        record_project_monitor_summary(
                            store=store,
                            summary=summary,
                        )
                    finally:
                        store.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "advisor: failed to record monitor summary for %s",
                    project_key,
                    exc_info=True,
                )

        results.append(entry)

    # Persist the state (last_tick_at, plus anything mutated per project).
    save_state(base_dir, state)

    return {
        "fired": True,
        "reason": "ok",
        "now_utc": now_utc.isoformat(),
        "tracked": [p for p, _ in projects],
        "enqueued": enqueued_projects,
        "results": results,
    }


# ---------------------------------------------------------------------------
# last_run bookkeeping — ad02 calls this after a session completes.
# ---------------------------------------------------------------------------


def mark_last_run(base_dir: Path, project_key: str, *, at: str | None = None) -> None:
    """Stamp ``last_run`` on the project after a completed advisor session.

    Called by the assess/history path — NOT by the tick handler — so a
    crashed mid-run session doesn't swallow the delta it was about to
    review. The next tick will re-detect the same signals and try again.
    """
    state = load_state(base_dir)
    proj = state.get(project_key)
    proj.last_run = at or iso_utc_now()
    save_state(base_dir, state)
