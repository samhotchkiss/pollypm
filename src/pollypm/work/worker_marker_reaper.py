"""Reaper for stale ``.pollypm/worker-markers/*.fresh`` files.

Background
----------
``SessionManager._launch_worker_window`` writes a per-task fresh-launch
marker at ``<project>/.pollypm/worker-markers/<window_name>.fresh`` so
``TmuxSessionService.create`` knows to send the kickoff string as
initial input on first boot. The marker is removed on the *happy
path* — only after the kickoff has been sent and verified
(``session_services/tmux.py`` around the ``fresh_launch_marker.unlink``
call).

The legacy implementation never removed the marker on any of the
sad-path branches:

* Identity-stacking guards rejected the kickoff before the unlink.
* ``_prepare_initial_input`` raised ``persona_swap_detected`` and the
  send was skipped (the comment promised "next correct send-tuple
  cleans up", which never happens once the task is deleted/abandoned).
* ``SessionService.create`` raised before the unlink.
* The ``work_tasks`` row was deleted/cancelled out from under the
  marker (the savethenovel orphan that prompted #1338).

Compare ``_bootstrap_clear_markers`` in ``supervisor.py`` for the
asymmetric ``session-markers/`` reaper that already runs at supervisor
boot.

This module owns:

* :func:`reap_orphan_worker_markers` — pure, side-effect-light helper
  that walks every project's ``.pollypm/worker-markers/`` directory
  and unlinks ``*.fresh`` files whose backing tmux window is dead OR
  whose ``work_tasks`` row is missing / terminal. Returns the list of
  markers it reaped so callers can log / emit events.

* :func:`sweep_worker_markers` — runtime variant that also kills the
  (probably-dead) tmux window when a marker has no matching
  ``work_tasks`` row. Mirrors the bootstrap reaper but is safe to call
  from the heartbeat sweep without restarting the supervisor.

Both helpers are intentionally defensive: if the projects table or
work_tasks table is missing (fresh install, mid-migration), they
no-op for that project rather than raising.

The structured logging / print events at reap callsites are placeholder
hooks for the audit-log infrastructure shipping in parallel; converting
them to audit-log events is a one-line change once that lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pollypm.work.task_state import (
    TERMINAL_TASK_STATUSES,
    parse_task_window_name,
)
from pollypm.storage.work_task_state import task_status_probe

logger = logging.getLogger(__name__)


# Statuses that mean "this worker marker should be reaped".
#
# The work-service only stores ``done``/``cancelled`` as terminal in
# ``work_status``, but the reaper accepts ``abandoned`` too — defensive
# in case a future migration promotes the execution-status value to a
# task-level field, and harmless today (the comparison is a frozenset
# membership test).
WORKER_MARKER_REAPABLE_STATUSES: frozenset[str] = (
    TERMINAL_TASK_STATUSES | frozenset({"abandoned"})
)


@dataclass(frozen=True, slots=True)
class ReapedMarker:
    """Record of a worker marker that was unlinked by the reaper.

    Surfaces enough context for log lines / future audit-log events:
    the project the marker lived under, the tmux window name encoded
    in the file stem, the on-disk path, and the reason the reaper
    decided to remove it.
    """

    project_key: str
    window_name: str
    marker_path: Path
    reason: str


def _iter_worker_markers(project_path: Path) -> Iterable[Path]:
    """Yield ``*.fresh`` files inside ``project_path/.pollypm/worker-markers``.

    Returns an empty iterator (rather than raising) when the directory
    is missing — fresh installs and projects that have never claimed a
    task simply have nothing to reap.
    """
    marker_dir = project_path / ".pollypm" / "worker-markers"
    try:
        if not marker_dir.is_dir():
            return ()
        return tuple(marker_dir.glob("*.fresh"))
    except OSError as exc:
        logger.debug(
            "worker_marker_reaper: cannot list %s: %s", marker_dir, exc,
        )
        return ()


def _live_worker_window_names(
    tmux: Any | None, storage_session: str | None,
) -> set[str]:
    """Return the set of live ``task-*`` window names in the storage closet.

    Returns an empty set on any tmux error — the reaper then errs on
    the side of treating windows as dead and reaping their markers.
    Callers that want a softer "tmux unreachable, skip everything"
    behaviour should check ``tmux.has_session`` themselves first.
    """
    if tmux is None or not storage_session:
        return set()
    try:
        if not tmux.has_session(storage_session):
            return set()
    except Exception:  # noqa: BLE001
        return set()
    try:
        windows = tmux.list_windows(storage_session)
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for window in windows:
        name = getattr(window, "name", None)
        if isinstance(name, str):
            out.add(name)
    return out


def _classify_marker(
    *,
    marker: Path,
    project_key: str,
    project_path: Path,
    workspace_root: Path | None,
    live_window_names: set[str],
) -> ReapedMarker | None:
    """Return a :class:`ReapedMarker` when ``marker`` should be reaped, else None.

    A marker is reapable when EITHER:

    * The task row is missing from ``work_tasks`` (orphan — task was
      cancelled/deleted out from under the marker), OR
    * The task row's ``work_status`` is in
      :data:`WORKER_MARKER_REAPABLE_STATUSES`, OR
    * The task row exists in a non-terminal state but the corresponding
      tmux window is dead (the savethenovel case — the worker session
      crashed and the marker never got its happy-path unlink).

    The window-name → ``(project, task_number)`` parse is shared with
    the inbox / cockpit so the reaper stays in sync with the launch
    side of the contract.
    """
    window_name = marker.stem  # ``task-<project>-<N>.fresh`` → ``task-<project>-<N>``
    parsed = parse_task_window_name(window_name)
    if parsed is None:
        # Marker file with an unrecognised name. Don't touch — better
        # to leak one marker than nuke a future format we don't
        # understand yet.
        return None
    parsed_project, task_number = parsed

    found_db, status = task_status_probe(
        project_key=parsed_project,
        task_number=task_number,
        project_path=project_path,
        workspace_root=workspace_root,
    )

    if not found_db:
        # No DB at all (fresh install or mid-migration). Be defensive
        # and skip — we don't have evidence the marker is actually
        # orphaned, only that we can't tell.
        return None

    if status is None:
        return ReapedMarker(
            project_key=project_key,
            window_name=window_name,
            marker_path=marker,
            reason="orphan: no work_tasks row",
        )

    if status in WORKER_MARKER_REAPABLE_STATUSES:
        return ReapedMarker(
            project_key=project_key,
            window_name=window_name,
            marker_path=marker,
            reason=f"task in terminal status '{status}'",
        )

    if window_name not in live_window_names:
        return ReapedMarker(
            project_key=project_key,
            window_name=window_name,
            marker_path=marker,
            reason=(
                f"tmux window missing for non-terminal task "
                f"(status='{status}')"
            ),
        )

    return None


def _resolve_workspace_root(config: Any) -> Path | None:
    """Pull ``project.workspace_root`` off the config, defensively."""
    project_settings = getattr(config, "project", None)
    workspace_root = getattr(project_settings, "workspace_root", None)
    if workspace_root is None:
        return None
    return Path(workspace_root)


_STORAGE_CLOSET_SESSION_SUFFIX = "-storage-closet"


def _resolve_storage_session(config: Any) -> str | None:
    """Mirror ``Supervisor.storage_closet_session_name`` without the import.

    The supervisor builds the storage-closet session name as
    ``<tmux_session>-storage-closet``. We duplicate the suffix here so
    the reaper can run in the heartbeat path without circular imports
    against ``Supervisor`` — covered by a regression test in
    ``tests/test_worker_marker_reaper.py``.
    """
    project_settings = getattr(config, "project", None)
    base = getattr(project_settings, "tmux_session", None)
    if not base:
        return None
    return f"{base}{_STORAGE_CLOSET_SESSION_SUFFIX}"


def reap_orphan_worker_markers(
    config: Any,
    *,
    tmux: Any | None = None,
) -> list[ReapedMarker]:
    """Bootstrap-time reaper. Walk every known project and unlink orphan markers.

    Mirrors :meth:`Supervisor._bootstrap_clear_markers` for the
    ``session-markers/`` directory. Designed to be called once at
    supervisor startup to clear the legacy backlog of leaked markers
    accumulated by builds prior to #1338.

    The function is logging-only on the destructive path: each
    reap emits a ``logging.warning`` so leak rates can be spotted in
    the cockpit log without a separate audit-log dependency.
    """
    projects = getattr(config, "projects", None) or {}
    if not projects:
        return []

    workspace_root = _resolve_workspace_root(config)
    storage_session = _resolve_storage_session(config)
    live_windows = _live_worker_window_names(tmux, storage_session)

    reaped: list[ReapedMarker] = []
    for project_key, project in projects.items():
        project_path = getattr(project, "path", None)
        if project_path is None:
            continue
        project_path = Path(project_path)
        for marker in _iter_worker_markers(project_path):
            decision = _classify_marker(
                marker=marker,
                project_key=str(project_key),
                project_path=project_path,
                workspace_root=workspace_root,
                live_window_names=live_windows,
            )
            if decision is None:
                continue
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "worker_marker_reaper: failed to unlink %s: %s",
                    marker, exc,
                )
                continue
            logger.warning(
                "worker_marker_reaper: reaped %s (project=%s, window=%s, reason=%s)",
                decision.marker_path,
                decision.project_key,
                decision.window_name,
                decision.reason,
            )
            # Placeholder for the parallel audit-log infra. When that
            # ships, replace this print with an audit event.
            print(
                f"[worker_marker_reaper] reaped {decision.window_name} "
                f"in {decision.project_key}: {decision.reason}",
                flush=True,
            )
            reaped.append(decision)
    return reaped


def sweep_worker_markers(
    config: Any,
    *,
    tmux: Any | None = None,
) -> list[ReapedMarker]:
    """Runtime sweep — reap orphan markers AND kill any matching dead windows.

    Same orphan classification as :func:`reap_orphan_worker_markers`,
    but additionally calls ``tmux.kill_window`` on the dead pane (if
    any) for each reaped marker. Safe to call from the heartbeat
    sweep without restarting the supervisor — the per-task workers
    already tolerate their session being killed via
    ``release_stale_claim``.

    Returns the list of markers reaped so callers can log / emit
    events. ``tmux.kill_window`` failures are swallowed (best-effort);
    the marker is unlinked regardless so the next claim can re-issue
    a fresh kickoff.
    """
    reaped = reap_orphan_worker_markers(config, tmux=tmux)
    if not reaped or tmux is None:
        return reaped
    storage_session = _resolve_storage_session(config)
    if not storage_session:
        return reaped
    for entry in reaped:
        target = f"{storage_session}:{entry.window_name}"
        try:
            tmux.kill_window(target)
            logger.info(
                "worker_marker_reaper: killed stale tmux window %s", target,
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort; the window was probably already dead, which
            # is exactly why we're reaping the marker in the first place.
            logger.debug(
                "worker_marker_reaper: kill_window(%s) failed: %s",
                target, exc,
            )
    return reaped


__all__ = [
    "ReapedMarker",
    "WORKER_MARKER_REAPABLE_STATUSES",
    "reap_orphan_worker_markers",
    "sweep_worker_markers",
]
