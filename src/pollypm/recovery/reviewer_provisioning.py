"""Auto-provision reviewer sessions for tracked projects (#1413).

Background
----------
Per-project worker sessions get auto-provisioned via
``SessionManager.provision_worker`` when a task transitions to a worker
node and no live session exists. Architect sessions get auto-spawned at
``pm project new`` via ``project_planning.cli.project._auto_spawn_architect``.

Reviewers had no equivalent path. ``savethenovel`` registered with
``tracked=true``, got a worker provisioned on demand, but its
``reviewer-savethenovel`` session was never created — so the first task
to land at ``code_review`` sat there forever (and dependency-blocked
every successor task).

This module fixes that with two complementary entry points:

* :func:`ensure_reviewer_sessions_for_tracked_projects` — bootstrap-time
  sweep called from :meth:`Supervisor.bootstrap_tmux` BEFORE
  ``plan_launches`` reads the session table. Iterates every tracked
  project and ensures a ``reviewer_<project>`` session exists in the
  config.
* :func:`ensure_reviewer_session_for_project` — on-demand entry point
  called from the work-service transition path when a task lands at a
  review node. Mirrors the worker provisioning hook in
  ``service_transition_manager.complete_node``.

Both paths are idempotent: a reviewer session that already exists in
config is left alone. Both emit ``session.provisioned`` audit events
when they actually create a new session, so the watchdog (#1414) can
quantify provisioning activity.

Constraints
-----------
* Best-effort: failures are logged and swallowed. The bootstrap path
  must not block cockpit startup if (e.g.) one project's reviewer
  account routing is broken — the other tracked projects still get
  their sessions and the heartbeat's existing ``no_session`` recovery
  catches the stragglers.
* Does NOT touch architect or worker provisioning — that's #1413's
  hard constraint. Pure additive scope.
* Workers continue to be provisioned per-task; this module only owns
  the long-lived reviewer-per-project lane.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ``reason`` values accepted by the audit emit. Other callers can extend
# this list as needed; the watchdog treats unknown reasons as "other".
REASON_BOOTSTRAP = "bootstrap"
REASON_ON_DEMAND_REVIEW = "on_demand_review"


def _emit_provisioned(
    *,
    project: str,
    role: str,
    reason: str,
    session_name: str,
    project_path: Path | None,
    status: str = "ok",
    detail: str = "",
) -> None:
    """Best-effort emit of the ``session.provisioned`` audit event.

    The watchdog (#1414) reads this stream to detect tracked projects
    that shipped without a reviewer session. Audit failures must never
    block the provisioning path — a missed event is strictly better
    than a failed bootstrap.
    """
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_SESSION_PROVISIONED

        _audit_emit(
            event=EVENT_SESSION_PROVISIONED,
            project=project,
            subject=session_name,
            actor="system",
            status=status,
            metadata={
                "role": role,
                "project": project,
                "reason": reason,
                "detail": detail,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001 — audit must never block provisioning
        logger.debug(
            "reviewer_provisioning: audit emit failed for %s/%s",
            project, role, exc_info=True,
        )


def _existing_reviewer_session(config: Any, project_key: str) -> Any | None:
    """Return the enabled reviewer session for ``project_key``, or None."""
    for session in getattr(config, "sessions", {}).values():
        if (
            getattr(session, "role", None) == "reviewer"
            and getattr(session, "project", None) == project_key
            and getattr(session, "enabled", True)
        ):
            return session
    return None


def ensure_reviewer_session_for_project(
    config_path: Path,
    project_key: str,
    *,
    reason: str = REASON_ON_DEMAND_REVIEW,
    launch: bool = True,
) -> tuple[bool, str]:
    """Ensure a reviewer session exists for ``project_key``.

    Returns ``(created, detail)``. ``created`` is True when this call
    actually registered a new session in config (or launched a stale
    one); False when the session already existed and we left it alone.
    The ``detail`` string carries either the session name (success) or
    the failure reason.

    Idempotent. Safe to call multiple times for the same project — the
    second call sees the session and returns ``(False, "already_exists")``.

    The ``reason`` is recorded on the audit event so the watchdog can
    distinguish bootstrap-time provisioning from on-demand catch-up.
    Set ``launch=False`` from contexts where the supervisor will launch
    the new session itself (e.g. bootstrap, where ``plan_launches``
    will pick up the session config and ``_bootstrap_launches`` will
    create the window).
    """
    try:
        from pollypm.config import load_config
    except Exception as exc:  # noqa: BLE001
        return False, f"import failed: {exc}"

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"load_config failed: {exc}"

    project = getattr(config, "projects", {}).get(project_key)
    if project is None:
        return False, f"unknown project: {project_key}"

    project_path = getattr(project, "path", None)

    existing = _existing_reviewer_session(config, project_key)
    if existing is not None:
        return False, f"already_exists:{existing.name}"

    # Mirror the no_session_spawn path: route through ``create_worker_session``
    # with role="reviewer" so account routing, worktree provisioning, and
    # window-name conventions all match the existing reviewer/architect
    # creation paths.
    try:
        from pollypm.workers import create_worker_session, launch_worker_session
    except Exception as exc:  # noqa: BLE001
        return False, f"import workers failed: {exc}"

    try:
        session = create_worker_session(
            config_path,
            project_key=project_key,
            prompt=None,
            role="reviewer",
        )
    except Exception as exc:  # noqa: BLE001
        _emit_provisioned(
            project=project_key,
            role="reviewer",
            reason=reason,
            session_name=f"reviewer_{project_key}",
            project_path=project_path,
            status="error",
            detail=f"create_worker_session failed: {exc}",
        )
        return False, f"create_worker_session failed: {exc}"

    launched_ok = True
    launch_error: str | None = None
    if launch:
        try:
            launch_worker_session(config_path, session.name)
        except Exception as exc:  # noqa: BLE001
            launched_ok = False
            launch_error = str(exc)
            logger.info(
                "reviewer_provisioning: %s registered but not launched (%s). "
                "Heartbeat / no_session recovery will pick it up.",
                session.name, exc,
            )

    _emit_provisioned(
        project=project_key,
        role="reviewer",
        reason=reason,
        session_name=session.name,
        project_path=project_path,
        status="ok" if launched_ok else "warn",
        detail=(
            f"session={session.name}"
            if launched_ok
            else f"session={session.name} launch_error={launch_error}"
        ),
    )
    return True, f"session={session.name}"


def ensure_reviewer_sessions_for_tracked_projects(
    config_path: Path,
    *,
    launch: bool = False,
) -> list[tuple[str, bool, str]]:
    """Sweep every tracked project and ensure a reviewer session exists.

    Called from :meth:`Supervisor.bootstrap_tmux` BEFORE ``plan_launches``
    so any newly-registered reviewer session is included in the bootstrap
    launch plan. ``launch`` defaults to False at bootstrap because
    ``_bootstrap_launches`` will create the window from ``plan_launches``.

    Returns ``[(project_key, created, detail), ...]`` for the caller to
    log / aggregate. Per-project failures are recorded as
    ``(project_key, False, "<error>")`` and don't abort the sweep —
    one broken project must not block reviewer provisioning for the
    rest of the workspace.
    """
    try:
        from pollypm.config import load_config
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reviewer_provisioning: load_config import failed: %s", exc,
        )
        return []

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reviewer_provisioning: load_config failed: %s", exc,
        )
        return []

    results: list[tuple[str, bool, str]] = []
    projects = getattr(config, "projects", {})
    for project_key, project in projects.items():
        if not getattr(project, "tracked", True):
            # Untracked projects opt out of the auto-provision sweep
            # (matches the cockpit visibility model — see
            # project_v1_tag and tracked_filter_asymmetry notes).
            results.append((project_key, False, "not_tracked"))
            continue
        try:
            created, detail = ensure_reviewer_session_for_project(
                config_path,
                project_key,
                reason=REASON_BOOTSTRAP,
                launch=launch,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reviewer_provisioning: ensure failed for %s: %s",
                project_key, exc,
            )
            results.append((project_key, False, f"error:{exc}"))
            continue
        results.append((project_key, created, detail))
    return results


__all__ = [
    "REASON_BOOTSTRAP",
    "REASON_ON_DEMAND_REVIEW",
    "ensure_reviewer_session_for_project",
    "ensure_reviewer_sessions_for_tracked_projects",
]
