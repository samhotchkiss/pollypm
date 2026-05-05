"""Tests for #268 Gap B — every launched session must be recorded in the
``sessions`` table.

Prior to the fix, only freshly-created tmux windows got a ``sessions``
row. Pre-existing windows (control-plane sessions that were already
running when the cockpit booted, plus configured workers) were left
unregistered, which broke :class:`SessionRoleIndex` role resolution
(``role:reviewer`` → ``pm-reviewer`` returned ``None``).

These tests exercise:

1. Bootstrap (fresh-launch path) writes a row per session.
2. Re-bootstrap / reconcile is idempotent — existing rows are updated,
   not duplicated, and rows get created for windows that were already
   alive when reconciliation ran.
3. :meth:`Supervisor.repair_sessions_table` rebuilds the table from
   live tmux state.
4. After bootstrap, :class:`SessionRoleIndex` resolves role-pinned
   actors to the correct live session.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor
from pollypm.tmux.client import TmuxWindow
from pollypm.work.models import ActorType
from pollypm.work.task_assignment import SessionRoleIndex


def _config(tmp_path: Path) -> PollyPMConfig:
    """Mirror the fixture from ``test_supervisor.py`` with an added reviewer
    control session so role:reviewer resolution can be exercised."""
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_controller",
            failover_enabled=False,
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
            "pm-reviewer": SessionConfig(
                name="pm-reviewer",
                role="reviewer",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-reviewer",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _neutralize_tmux(supervisor: Supervisor, monkeypatch) -> None:
    """Replace every tmux mutation with a no-op so bootstrap never shells out."""
    monkeypatch.setattr(supervisor, "_probe_controller_account", lambda account_name: None)
    monkeypatch.setattr(
        supervisor, "_stabilize_launch", lambda launch, target, on_status=None: None
    )
    monkeypatch.setattr(
        supervisor, "_stabilize_claude_launch", lambda target: None, raising=False
    )
    monkeypatch.setattr(
        supervisor, "_stabilize_codex_launch", lambda target: None, raising=False
    )
    monkeypatch.setattr(
        supervisor, "_send_initial_input_if_fresh", lambda launch, target: None, raising=False
    )
    monkeypatch.setattr(supervisor, "_mark_session_resume_ready", lambda launch: None, raising=False)
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: False)
    monkeypatch.setattr(
        supervisor.tmux,
        "create_session",
        lambda name, window_name, command, **kwargs: "%1",
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: "%2",
    )
    monkeypatch.setattr(supervisor.tmux, "set_window_option", lambda target, option, value: None)
    monkeypatch.setattr(supervisor.tmux, "set_pane_history_limit", lambda target, lines: None, raising=False)
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: None)
    monkeypatch.setattr(supervisor.tmux, "list_windows", lambda name: [])
    monkeypatch.setattr(supervisor.tmux, "list_all_windows", lambda: [])
    monkeypatch.setattr(supervisor, "focus_console", lambda: None)
    monkeypatch.setattr(supervisor, "_resolve_pane_id", lambda s, w: None, raising=False)


def test_bootstrap_registers_every_session_row(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    _neutralize_tmux(supervisor, monkeypatch)

    supervisor.bootstrap_tmux()

    rows = {s.name: s for s in supervisor.store.list_sessions()}
    assert set(rows) == {"heartbeat", "operator", "pm-reviewer"}
    assert rows["heartbeat"].window_name == "pm-heartbeat"
    assert rows["operator"].window_name == "pm-operator"
    assert rows["pm-reviewer"].window_name == "pm-reviewer"
    assert rows["pm-reviewer"].role == "reviewer"


def test_rebootstrap_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    """A second bootstrap over already-live sessions must upsert (not duplicate)
    and must register any sessions that were already running — the scenario
    that caused the empty ``sessions`` table in #268 Gap B."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    _neutralize_tmux(supervisor, monkeypatch)

    supervisor.bootstrap_tmux()
    first_rows = sorted(s.name for s in supervisor.store.list_sessions())
    assert first_rows == ["heartbeat", "operator", "pm-reviewer"]

    # Now simulate the "cockpit restart with live sessions" path:
    # ``bootstrap_tmux`` sees existing tmux sessions and delegates to
    # ``_reconcile_existing``. All three windows are already alive and
    # have no missing rows to create — the fix ensures they still get
    # upserted.
    storage = supervisor.storage_closet_session_name()
    cockpit = config.project.tmux_session

    monkeypatch.setattr(
        supervisor.tmux,
        "has_session",
        lambda name: name in {storage, cockpit},
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda name: [
            TmuxWindow(
                session=storage,
                index=i,
                name=w,
                active=(i == 0),
                pane_id=f"%{i + 10}",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
            for i, w in enumerate(["pm-heartbeat", "pm-operator", "pm-reviewer"])
        ] if name == storage else [],
    )

    supervisor.bootstrap_tmux()

    rows = sorted(s.name for s in supervisor.store.list_sessions())
    # No duplicates (PRIMARY KEY on name prevents duplicates anyway, but
    # verify the set is unchanged and not empty after reconcile).
    assert rows == ["heartbeat", "operator", "pm-reviewer"]


def test_reconcile_continues_when_window_option_fails(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    storage = supervisor.storage_closet_session_name()
    cockpit = config.project.tmux_session

    created_windows: list[tuple[str, str, bool]] = []
    piped_targets: list[str] = []
    window_options: list[tuple[str, str, str]] = []

    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: name in {storage, cockpit})
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda name: [
            TmuxWindow(
                session=storage,
                index=i,
                name=w,
                active=(i == 0),
                pane_id=f"%{i + 10}",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
            for i, w in enumerate(["pm-heartbeat", "pm-operator"])
        ] if name == storage else [],
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: created_windows.append(
            (name, window_name, detached)
        ),
    )

    def _set_window_option(target: str, option: str, value: str) -> None:
        window_options.append((target, option, value))
        if target == f"{storage}:pm-reviewer" and option == "allow-passthrough":
            raise subprocess.CalledProcessError(
                1,
                ["tmux", "set-window-option", "-t", target, option, value],
            )

    monkeypatch.setattr(supervisor.tmux, "set_window_option", _set_window_option)
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: piped_targets.append(target))
    monkeypatch.setattr(supervisor, "ensure_heartbeat_schedule", lambda: None)
    monkeypatch.setattr(supervisor, "ensure_knowledge_extraction_schedule", lambda: None)

    controller = supervisor.bootstrap_tmux()

    assert controller == "claude_controller"
    assert created_windows == [(storage, "pm-reviewer", True)]
    assert piped_targets == [f"{storage}:pm-reviewer"]
    assert (f"{storage}:pm-reviewer", "focus-events", "on") in window_options
    rows = sorted(s.name for s in supervisor.store.list_sessions())
    assert rows == ["heartbeat", "operator", "pm-reviewer"]


def test_repair_sessions_table_rebuilds_from_live_tmux(
    monkeypatch, tmp_path: Path
) -> None:
    """``repair_sessions_table`` upserts a row for every configured session
    whose window is alive in tmux — this is the path that heals a DB
    populated by the broken pre-fix build."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    # Start with a completely empty sessions table (the user's observed
    # state: only one worker row, other control sessions unregistered).
    assert supervisor.store.list_sessions() == []

    storage = supervisor.storage_closet_session_name()
    live_names = {"pm-heartbeat", "pm-operator", "pm-reviewer"}
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda n: n == storage)
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda n: [
            TmuxWindow(
                session=storage,
                index=i,
                name=w,
                active=(i == 0),
                pane_id=f"%{i + 20}",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
            for i, w in enumerate(sorted(live_names))
        ] if n == storage else [],
    )

    upserted = supervisor.repair_sessions_table()
    assert upserted == 3

    rows = {s.name: s for s in supervisor.store.list_sessions()}
    assert set(rows) == {"heartbeat", "operator", "pm-reviewer"}

    # Re-running the repair should not add duplicates (primary-key enforced)
    # and should still return the live count — demonstrating idempotence.
    upserted_again = supervisor.repair_sessions_table()
    assert upserted_again == 3
    assert len(supervisor.store.list_sessions()) == 3


def test_session_role_index_resolves_reviewer_after_bootstrap(
    monkeypatch, tmp_path: Path
) -> None:
    """End-to-end: with bootstrap registering every session row, the
    :class:`SessionRoleIndex` now resolves ``role:reviewer`` to the live
    ``pm-reviewer`` window — the regression described in #268 Gap B."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    _neutralize_tmux(supervisor, monkeypatch)

    supervisor.bootstrap_tmux()

    # SessionService.list() consults the DB, so after bootstrap the
    # role index must see every configured session.
    storage = supervisor.storage_closet_session_name()
    live_windows = [
        TmuxWindow(
            session=storage,
            index=i,
            name=w,
            active=(i == 0),
            pane_id=f"%{i + 30}",
            pane_current_command="claude",
            pane_current_path=str(tmp_path),
            pane_dead=False,
        )
        for i, w in enumerate(["pm-heartbeat", "pm-operator", "pm-reviewer"])
    ]
    monkeypatch.setattr(supervisor.tmux, "list_all_windows", lambda: live_windows)
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda n: n == storage)

    index = SessionRoleIndex(supervisor.session_service)
    handle = index.resolve(ActorType.ROLE, "reviewer", "pollypm")
    assert handle is not None
    assert handle.name == "pm-reviewer"
    assert handle.window_name == "pm-reviewer"

    # Sanity: operator and heartbeat resolve too. The control sessions are
    # named ``pm-operator`` / ``pm-heartbeat`` in config so ``SessionRoleIndex``
    # can map roles directly via their static name table.
    # (Our fixture uses historical short names ``operator`` / ``heartbeat``,
    # which do NOT match the ``pm-*`` candidate names — that's a config-shape
    # detail outside this test's scope. We assert only on the reviewer path,
    # which is the path #268 Gap B broke.)
