"""Regression for #1069 — task_assignment.{sweep,notify} fd leak.

#1067 fixed two leak paths in ``core_recurring`` (session.health_sweep
and alerts.gc) that each constructed a fresh ``Supervisor(config)``
without releasing it. Verifying that fix on an idle daemon showed
0 fd/min growth, but on a busy daemon (live task work, rework state
transitions) the post-#1067 daemon still grew the user-scope
``state.db`` fd count at ~9.5/min.

Root cause: ``task_assignment_notify.resolver.load_runtime_services``
opens a fresh ``StateStore`` on every call and a fresh
``PgWorkService``. The cadence handlers
(``task_assignment.sweep`` @every 30s, ``task_assignment.notify``
on every state transition incl. rework/notify_rejection,
``pane.classify`` @every 30s) used to close only the work service
and leaked the state-store sqlite fd + WAL handles per tick.

This test runs each of the three handlers ~50 times against a
config-backed tmpdir and asserts the parent process's fd count
stays flat. A leak of one connection per call would surface as
~50 extra fds and fail the assertion.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pollypm.config import write_config
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


_DEV_FD = Path("/dev/fd")


def _fd_count() -> int:
    return len(os.listdir(_DEV_FD))


def _build_config(tmp_path: Path) -> Path:
    """Materialise a minimal PollyPM config + state.db on disk."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".pollypm").mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
            workspace_root=project_root,
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm/homes/claude_main",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            )
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = project_root / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


@pytest.mark.skipif(not _DEV_FD.exists(), reason="needs /dev/fd to sample fd count")
def test_task_assignment_sweep_handler_does_not_leak_fds(tmp_path: Path) -> None:
    """``task_assignment.sweep`` must close the StateStore it opens.

    Before #1069 this handler called ``load_runtime_services()`` which
    opens a fresh ``StateStore`` (sqlite connection + WAL handles) on
    every invocation, and only ``services.work_service`` was closed.
    Under the @every 30s cadence that drove sustained fd growth on
    busy daemons.
    """
    config_path = _build_config(tmp_path)

    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        task_assignment_sweep_handler,
    )

    payload = {"config_path": str(config_path)}
    # Warm-up so any process-global lazy imports / cached engines are
    # already established before we sample the baseline.
    task_assignment_sweep_handler(payload)
    task_assignment_sweep_handler(payload)

    baseline = _fd_count()
    for _ in range(50):
        task_assignment_sweep_handler(payload)
    after = _fd_count()

    # Allow a small constant slack for any one-shot cached resources
    # the WAL pragma touches during the loop (e.g. a -wal handle held
    # by the singleton msg_store from get_store). A real per-call
    # leak would be ≥50 here.
    assert after - baseline <= 3, (
        f"fd count grew from {baseline} to {after} over 50 sweep ticks "
        f"— task_assignment.sweep is leaking sqlite connections "
        f"(see #1069 / resolver.load_runtime_services)"
    )


@pytest.mark.skipif(not _DEV_FD.exists(), reason="needs /dev/fd to sample fd count")
def test_task_assignment_notify_handler_does_not_leak_fds(tmp_path: Path) -> None:
    """``task_assignment.notify`` must close the StateStore it opens.

    The notify handler runs on every state transition (including
    rework / notify_rejection / approve). Even on a quiet day a busy
    project triggers it dozens of times in minutes, so a one-fd
    leak per call adds up fast.
    """
    config_path = _build_config(tmp_path)

    from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
        task_assignment_notify_handler,
    )

    # A minimally valid notify payload — the handler short-circuits at
    # ``no_session`` because no tmux session matches, but it still has
    # to open + close the runtime services on every call. That's the
    # path we're guarding.
    payload = {
        "task_id": "demo/1",
        "project": "demo",
        "task_number": 1,
        "title": "smoketest",
        "current_node": "work",
        "current_node_kind": "work",
        "actor_type": "role",
        "actor_name": "worker",
        "work_status": "queued",
        "priority": "normal",
        "transitioned_at": "2026-05-03T00:00:00",
        "transitioned_by": "system",
        "config_path": str(config_path),
    }
    # Warm-up.
    task_assignment_notify_handler(payload)
    task_assignment_notify_handler(payload)

    baseline = _fd_count()
    for _ in range(50):
        task_assignment_notify_handler(payload)
    after = _fd_count()

    assert after - baseline <= 3, (
        f"fd count grew from {baseline} to {after} over 50 notify calls "
        f"— task_assignment.notify is leaking sqlite connections "
        f"(see #1069 / resolver.load_runtime_services)"
    )


@pytest.mark.skipif(not _DEV_FD.exists(), reason="needs /dev/fd to sample fd count")
def test_pane_text_classify_handler_does_not_leak_fds(tmp_path: Path) -> None:
    """``pane.classify`` (@every 30s) shares the same leak shape."""
    config_path = _build_config(tmp_path)

    from pollypm.plugins_builtin.core_recurring.sweeps import (
        pane_text_classify_handler,
    )

    payload = {"config_path": str(config_path)}
    pane_text_classify_handler(payload)
    pane_text_classify_handler(payload)

    baseline = _fd_count()
    for _ in range(50):
        pane_text_classify_handler(payload)
    after = _fd_count()

    assert after - baseline <= 3, (
        f"fd count grew from {baseline} to {after} over 50 pane.classify "
        f"ticks — pane_text_classify_handler is leaking sqlite "
        f"connections (see #1069 / resolver.load_runtime_services)"
    )
