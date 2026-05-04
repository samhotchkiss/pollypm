from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pollypm.heartbeats.api import SupervisorHeartbeatAPI
from pollypm.heartbeats.base import HeartbeatCursor, HeartbeatSessionContext, HeartbeatUnmanagedWindow
from pollypm.heartbeats.local import LocalHeartbeatBackend, _collect_work_service_signals
from pollypm.memory_backends import get_memory_backend
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor
from pollypm.tmux.client import TmuxWindow


def _config(tmp_path: Path) -> PollyPMConfig:
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
            failover_accounts=[],
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
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
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


def _context(**overrides) -> HeartbeatSessionContext:
    data = {
        "session_name": "worker_pollypm",
        "role": "worker",
        "project_key": "pollypm",
        "provider": "claude",
        "account_name": "claude_controller",
        "cwd": "/workspace",
        "tmux_session": "pollypm",
        "window_name": "worker_pollypm",
        "source_path": "/tmp/worker.log",
        "source_bytes": 64,
        "transcript_delta": "Implemented the change.",
        "pane_text": "Implemented the change.",
        "snapshot_path": "/tmp/worker.txt",
        "snapshot_hash": "hash-1",
        "pane_id": "%1",
        "pane_command": "claude",
        "pane_dead": False,
        "window_present": True,
        "previous_log_bytes": 32,
        "previous_snapshot_hash": "hash-0",
        "cursor": None,
    }
    data.update(overrides)
    return HeartbeatSessionContext(**data)


class FakeHeartbeatAPI:
    def __init__(
        self,
        contexts: list[HeartbeatSessionContext],
        *,
        hashes: dict[str, list[str]] | None = None,
        unmanaged_windows: list[HeartbeatUnmanagedWindow] | None = None,
    ) -> None:
        self._contexts = contexts
        self._hashes = hashes or {}
        self._unmanaged_windows = unmanaged_windows or []
        self.alerts: dict[tuple[str, str], SimpleNamespace] = {}
        self.statuses: dict[str, tuple[str, str]] = {}
        self.cursor_updates: list[dict[str, object]] = []
        self.events: list[tuple[str, str, str]] = []
        self.observations: list[str] = []
        self.checkpoints: list[tuple[str, list[str]]] = []
        self.recoveries: list[tuple[str, str, str]] = []
        self.account_marks: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.supervisor = SimpleNamespace(
            config=SimpleNamespace(sessions={}, projects={}),
            msg_store=_FakeMessageStore(),
        )

    def list_sessions(self) -> list[HeartbeatSessionContext]:
        return list(self._contexts)

    def list_unmanaged_windows(self) -> list[HeartbeatUnmanagedWindow]:
        return list(self._unmanaged_windows)

    def get_cursor(self, session_name: str):
        return None

    def update_cursor(self, session_name: str, *, source_path: str, last_offset: int, snapshot_hash: str = "", verdict: str = "", reason: str = "") -> None:
        self.cursor_updates.append({
            "session_name": session_name,
            "source_path": source_path,
            "last_offset": last_offset,
            "snapshot_hash": snapshot_hash,
            "verdict": verdict,
            "reason": reason,
        })

    def record_observation(self, context: HeartbeatSessionContext) -> None:
        self.observations.append(context.session_name)

    def record_checkpoint(self, context: HeartbeatSessionContext, *, alerts: list[str]) -> None:
        self.checkpoints.append((context.session_name, alerts))

    def record_event(self, session_name: str, event_type: str, message: str) -> None:
        self.events.append((session_name, event_type, message))

    def raise_alert(self, session_name: str, alert_type: str, severity: str, message: str) -> None:
        self.alerts[(session_name, alert_type)] = SimpleNamespace(
            session_name=session_name,
            alert_type=alert_type,
            severity=severity,
            message=message,
        )

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.alerts.pop((session_name, alert_type), None)

    def open_alerts(self):
        return list(self.alerts.values())

    def set_session_status(self, session_name: str, status: str, *, reason: str = "") -> None:
        self.statuses[session_name] = (status, reason)

    def mark_account_auth_broken(self, account_name: str, provider: str, *, reason: str) -> None:
        self.account_marks.append((account_name, provider, reason))

    def recent_snapshot_hashes(self, session_name: str, *, limit: int = 3) -> list[str]:
        return self._hashes.get(session_name, [])[:limit]

    def recover_session(self, session_name: str, *, failure_type: str, message: str) -> None:
        self.recoveries.append((session_name, failure_type, message))

    def send_session_message(self, session_name: str, text: str, *, owner: str = "heartbeat") -> None:
        self.messages.append((session_name, text, owner))

    def queue_polly_followup(self, session_name: str, reason: str) -> None:
        self.messages.append(("operator", f"Heartbeat follow-up for {session_name}: {reason}", "heartbeat"))


class _FakeMessageStore:
    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []

    def query_messages(
        self,
        *,
        type: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        rows = [
            event
            for event in self._events
            if (type is None or event.get("type") == type)
            and (scope is None or event.get("scope") == scope)
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def append_event(
        self,
        *,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, object],
        created_at: datetime | None = None,
    ) -> None:
        self._events.append(
            {
                "type": "event",
                "scope": scope,
                "sender": sender,
                "subject": subject,
                "payload": payload,
                "created_at": created_at or datetime.now(timezone.utc),
            }
        )

    def upsert_alert(self, *_args, **_kwargs) -> None:
        return None


def _work_signal_api(tmp_path: Path, session_name: str = "worker_pollypm") -> SimpleNamespace:
    config = _config(tmp_path)
    config.sessions[session_name] = SessionConfig(
        name=session_name,
        role="worker",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="pollypm",
        window_name=session_name,
    )
    state_db = tmp_path / ".pollypm" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.touch()
    return SimpleNamespace(
        supervisor=SimpleNamespace(
            config=config,
            msg_store=SimpleNamespace(query_messages=lambda **_kwargs: []),
        )
    )


def test_supervisor_heartbeat_api_persists_cursor_and_reads_incremental_delta(tmp_path: Path, monkeypatch) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    launch = supervisor.plan_launches()[0]
    launch.log_path.parent.mkdir(parents=True, exist_ok=True)
    launch.log_path.write_text("alpha\nbeta\n")
    snapshot_path = supervisor.config.project.snapshots_dir / "pm-operator-test.txt"
    window = TmuxWindow(
        session="pollypm",
        index=1,
        name=launch.window_name,
        active=True,
        pane_id="%1",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # #1096 — window_map keys by (tmux_session, window_name) tuple.
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {(window.session, launch.window_name): window},
    )
    monkeypatch.setattr(supervisor, "write_snapshot", lambda _window, _lines: (snapshot_path, "snapshot\n"))

    api = SupervisorHeartbeatAPI(supervisor)
    context = api.list_sessions()[0]

    assert context.transcript_delta == "alpha\nbeta\n"

    api.update_cursor(
        context.session_name,
        source_path=context.source_path,
        last_offset=len("alpha\n"),
        snapshot_hash=context.snapshot_hash,
        verdict="done",
        reason="partial read",
    )

    api = SupervisorHeartbeatAPI(supervisor)
    context = api.list_sessions()[0]

    assert context.transcript_delta == "beta\n"
    cursor_path = supervisor.config.project.base_dir / "heartbeat" / "cursors.json"
    assert cursor_path.exists()
    assert "operator" in cursor_path.read_text()


def test_supervisor_heartbeat_api_lists_unmanaged_windows(tmp_path: Path, monkeypatch) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    launch = supervisor.plan_launches()[0]
    managed_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name=launch.window_name,
        active=True,
        pane_id="%1",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    console_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=0,
        name=supervisor.console_window_name(),
        active=True,
        pane_id="%2",
        pane_current_command="python3",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    unmanaged_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=2,
        name="e2e-sandbox",
        active=False,
        pane_id="%3",
        pane_current_command="claude",
        pane_current_path=str(tmp_path / "sandbox"),
        pane_dead=False,
    )
    # #1096 — window_map keys by (tmux_session, window_name) tuple.
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {
            (managed_window.session, managed_window.name): managed_window,
            (console_window.session, console_window.name): console_window,
            (unmanaged_window.session, unmanaged_window.name): unmanaged_window,
        },
    )

    api = SupervisorHeartbeatAPI(supervisor)

    assert api.list_unmanaged_windows() == [
        HeartbeatUnmanagedWindow(
            tmux_session=supervisor.config.project.tmux_session,
            window_name="e2e-sandbox",
            pane_id="%3",
            pane_command="claude",
            pane_dead=False,
            pane_path=str(tmp_path / "sandbox"),
        )
    ]


def test_supervisor_heartbeat_api_skips_hyphen_underscore_drift(
    tmp_path: Path, monkeypatch
) -> None:
    """#1056 — ``pm worker-start --role architect <project>`` registers a
    session named ``architect_<project>`` (underscore) and a tmux window
    named ``architect-<project>`` (hyphen). The unmanaged-window check
    must treat the two as equivalent so the live architect session
    doesn't get false-flagged while ``pm status`` reports it healthy.
    """
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    launch = supervisor.plan_launches()[0]
    managed_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name=launch.window_name,
        active=True,
        pane_id="%1",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    console_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=0,
        name=supervisor.console_window_name(),
        active=True,
        pane_id="%2",
        pane_current_command="python3",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # Architect session — hyphen-form window for an underscore-named
    # session config. This is the canonical worker-start spawn shape:
    #   session.name        = "architect_russell"
    #   session.window_name = "architect-russell"
    architect_session = SessionConfig(
        name="architect_russell",
        role="architect",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="russell",
        window_name="architect-russell",
    )
    supervisor.config.sessions["architect_russell"] = architect_session
    supervisor.invalidate_launch_cache()
    architect_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=2,
        name="architect-russell",
        active=False,
        pane_id="%3",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # #1096 — window_map keys by (tmux_session, window_name) tuple.
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {
            (managed_window.session, managed_window.name): managed_window,
            (console_window.session, console_window.name): console_window,
            (architect_window.session, architect_window.name): architect_window,
        },
    )

    api = SupervisorHeartbeatAPI(supervisor)
    assert api.list_unmanaged_windows() == []


def test_supervisor_heartbeat_api_normalizes_session_name_to_window(
    tmp_path: Path, monkeypatch
) -> None:
    """#1056 — even when a session config drops the explicit window_name
    and the tmux window is named after the underscore session name with
    a hyphen substitution (``architect-russell`` for session
    ``architect_russell``), the normalized lookup must keep the live
    window out of the unmanaged set.
    """
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    launch = supervisor.plan_launches()[0]
    managed_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name=launch.window_name,
        active=True,
        pane_id="%1",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    console_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=0,
        name=supervisor.console_window_name(),
        active=True,
        pane_id="%2",
        pane_current_command="python3",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # Session has no explicit window_name — the planner falls back to
    # session.name (``architect_russell``). The tmux window is named
    # ``architect-russell`` (hyphen). Without normalization the live
    # window would land in the unmanaged set.
    architect_session = SessionConfig(
        name="architect_russell",
        role="architect",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="russell",
        window_name=None,
    )
    supervisor.config.sessions["architect_russell"] = architect_session
    supervisor.invalidate_launch_cache()
    architect_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=2,
        name="architect-russell",
        active=False,
        pane_id="%3",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # #1096 — window_map keys by (tmux_session, window_name) tuple.
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {
            (managed_window.session, managed_window.name): managed_window,
            (console_window.session, console_window.name): console_window,
            (architect_window.session, architect_window.name): architect_window,
        },
    )

    api = SupervisorHeartbeatAPI(supervisor)
    assert api.list_unmanaged_windows() == []


def test_supervisor_heartbeat_api_skips_pm_upgrade_window(tmp_path: Path, monkeypatch) -> None:
    """#1000 — the rail one-click upgrade opens a ``pm-upgrade`` window
    so the user can read upgrade output and the changelog. It's
    intentional and short-lived; the supervisor must not flag it as
    ``unmanaged_window:pollypm:pm-upgrade``.
    """
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    launch = supervisor.plan_launches()[0]
    managed_window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name=launch.window_name,
        active=True,
        pane_id="%1",
        pane_current_command="claude",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    console_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=0,
        name=supervisor.console_window_name(),
        active=True,
        pane_id="%2",
        pane_current_command="python3",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    pm_upgrade_window = TmuxWindow(
        session=supervisor.config.project.tmux_session,
        index=3,
        name="pm-upgrade",
        active=False,
        pane_id="%4",
        pane_current_command="bash",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )
    # #1096 — window_map keys by (tmux_session, window_name) tuple.
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {
            (managed_window.session, managed_window.name): managed_window,
            (console_window.session, console_window.name): console_window,
            (pm_upgrade_window.session, pm_upgrade_window.name): pm_upgrade_window,
        },
    )

    api = SupervisorHeartbeatAPI(supervisor)

    assert api.list_unmanaged_windows() == []


def test_supervisor_heartbeat_api_records_snapshot_learnings_into_memory(tmp_path: Path, monkeypatch) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    monkeypatch.setattr("pollypm.knowledge_extract.run_haiku_json", lambda prompt: None)
    api = SupervisorHeartbeatAPI(supervisor)
    snapshot_path = supervisor.config.project.snapshots_dir / "pm-operator-test.txt"
    context = _context(
        session_name="operator",
        role="operator-pm",
        project_key="pollypm",
        cwd=str(tmp_path),
        window_name="pm-operator",
        source_path=str(tmp_path / ".pollypm" / "logs" / "operator.log"),
        snapshot_path=str(snapshot_path),
        pane_text=(
            "Decision: use the heartbeat snapshot as a memory source. "
            "Risk: duplicate learnings will pollute search. "
            "Idea: compact project memory after new learnings are stored."
        ),
        pane_command="claude",
    )

    api.record_checkpoint(context, alerts=[])

    backend = get_memory_backend(tmp_path, "file")
    decisions = backend.list_entries(scope="pollypm", kind="decision")
    risks = backend.list_entries(scope="pollypm", kind="risk")
    ideas = backend.list_entries(scope="pollypm", kind="idea")
    checkpoints = backend.list_entries(scope="pollypm", kind="checkpoint")

    # Knowledge extraction from snapshots was moved to session_intelligence
    # (unified Haiku call) — record_checkpoint no longer extracts learnings
    # into decisions/risks/ideas. It only records the checkpoint itself.
    assert len(checkpoints) == 1


def test_supervisor_heartbeat_api_deduplicates_snapshot_learnings(tmp_path: Path, monkeypatch) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    monkeypatch.setattr("pollypm.knowledge_extract.run_haiku_json", lambda prompt: None)
    api = SupervisorHeartbeatAPI(supervisor)
    snapshot_path = supervisor.config.project.snapshots_dir / "pm-operator-test.txt"
    context = _context(
        session_name="operator",
        role="operator-pm",
        project_key="pollypm",
        cwd=str(tmp_path),
        window_name="pm-operator",
        source_path=str(tmp_path / ".pollypm" / "logs" / "operator.log"),
        snapshot_path=str(snapshot_path),
        pane_text="Decision: keep heartbeat learnings deduplicated.",
        pane_command="claude",
    )

    api.record_checkpoint(context, alerts=[])
    api.record_checkpoint(context, alerts=[])

    backend = get_memory_backend(tmp_path, "file")
    checkpoints = backend.list_entries(scope="pollypm", kind="checkpoint")

    # Knowledge extraction moved to session_intelligence — record_checkpoint
    # only creates checkpoint entries now. Two calls = two checkpoints.
    assert len(checkpoints) == 2


def test_collect_work_service_signals_skips_git_log_for_unregistered_worktree(tmp_path: Path, monkeypatch) -> None:
    api = _work_signal_api(tmp_path)
    context = _context()
    claimed_worktree = tmp_path / ".pollypm" / "worktrees" / "pollypm-1"
    other_worktree = tmp_path / ".pollypm" / "worktrees" / "other"

    class FakeSQLiteWorkService:
        def __init__(self, *, db_path: Path, project_path: Path) -> None:
            self.db_path = db_path
            self.project_path = project_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def list_worker_sessions(self, *, project: str, active_only: bool = True):
            return [
                SimpleNamespace(
                    task_project=project,
                    task_number=1,
                    started_at="2026-04-21T00:00:00+00:00",
                    worktree_path=str(claimed_worktree),
                    branch_name="task/pollypm-1",
                )
            ]

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        assert kwargs.get("cwd") is None
        if list(args) == ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"]:
            stdout = f"worktree {other_worktree}\nHEAD deadbeef\nbranch refs/heads/task/other\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr("pollypm.work.sqlite_service.SQLiteWorkService", FakeSQLiteWorkService)
    monkeypatch.setattr("pollypm.heartbeats.local.subprocess.run", fake_run)

    signals = _collect_work_service_signals(api, context)

    assert signals["last_commit_seconds_ago"] is None
    assert calls == [
        (
            ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_collect_work_service_signals_reads_registered_worktree_commit_via_project_repo(tmp_path: Path, monkeypatch) -> None:
    api = _work_signal_api(tmp_path)
    context = _context()
    claimed_worktree = tmp_path / ".pollypm" / "worktrees" / "pollypm-1"

    class FakeSQLiteWorkService:
        def __init__(self, *, db_path: Path, project_path: Path) -> None:
            self.db_path = db_path
            self.project_path = project_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def list_worker_sessions(self, *, project: str, active_only: bool = True):
            return [
                SimpleNamespace(
                    task_project=project,
                    task_number=1,
                    started_at="2026-04-21T00:00:00+00:00",
                    worktree_path=str(claimed_worktree),
                    branch_name="task/pollypm-1",
                )
            ]

    calls: list[tuple[list[str], dict[str, object]]] = []
    commit_ts = 1_700_000_000
    head_oid = "abc123def456"

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        assert kwargs.get("cwd") is None
        if list(args) == ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"]:
            stdout = (
                f"worktree {claimed_worktree}\n"
                f"HEAD {head_oid}\n"
                "branch refs/heads/task/pollypm-1\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if list(args) == ["git", "-C", str(tmp_path), "log", "-1", "--format=%ct", head_oid]:
            return subprocess.CompletedProcess(args, 0, f"{commit_ts}\n", "")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr("pollypm.work.sqlite_service.SQLiteWorkService", FakeSQLiteWorkService)
    monkeypatch.setattr("pollypm.heartbeats.local.subprocess.run", fake_run)

    signals = _collect_work_service_signals(api, context)

    assert signals["last_commit_seconds_ago"] is not None
    assert calls == [
        (
            ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
        (
            ["git", "-C", str(tmp_path), "log", "-1", "--format=%ct", head_oid],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
    ]


def test_has_pending_work_skips_review_only_tasks(tmp_path: Path, monkeypatch) -> None:
    api = _work_signal_api(tmp_path)
    context = _context()
    backend = LocalHeartbeatBackend()

    class _NoTaskBackend:
        def exists(self) -> bool:
            return False

    class FakeSQLiteWorkService:
        def __init__(self, *, db_path: Path, project_path: Path) -> None:
            self.db_path = db_path
            self.project_path = project_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def list_tasks(self, *, project: str):
            return [
                SimpleNamespace(
                    task_id=f"{project}/1",
                    work_status=SimpleNamespace(value="review"),
                )
            ]

    monkeypatch.setattr(
        "pollypm.task_backends.get_task_backend",
        lambda _path: _NoTaskBackend(),
    )
    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService",
        FakeSQLiteWorkService,
    )

    assert backend._has_pending_work(api, context) is False


def test_has_pending_work_keeps_in_progress_tasks_actionable(tmp_path: Path, monkeypatch) -> None:
    api = _work_signal_api(tmp_path)
    context = _context()
    backend = LocalHeartbeatBackend()

    class _NoTaskBackend:
        def exists(self) -> bool:
            return False

    class FakeSQLiteWorkService:
        def __init__(self, *, db_path: Path, project_path: Path) -> None:
            self.db_path = db_path
            self.project_path = project_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def list_tasks(self, *, project: str):
            return [
                SimpleNamespace(
                    task_id=f"{project}/1",
                    work_status=SimpleNamespace(value="in_progress"),
                )
            ]

    monkeypatch.setattr(
        "pollypm.task_backends.get_task_backend",
        lambda _path: _NoTaskBackend(),
    )
    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService",
        FakeSQLiteWorkService,
    )

    assert backend._has_pending_work(api, context) is True


def test_local_heartbeat_backend_marks_followup_when_work_remains() -> None:
    api = FakeHeartbeatAPI([_context(transcript_delta="Implemented the parser. Next step: add coverage.")])

    LocalHeartbeatBackend().run(api)

    assert api.statuses["worker_pollypm"][0] == "needs_followup"
    assert ("worker_pollypm", "needs_followup") in api.alerts
    assert api.cursor_updates[-1]["verdict"] == "needs_followup"


def test_local_heartbeat_backend_marks_waiting_on_user_for_question() -> None:
    api = FakeHeartbeatAPI([_context(transcript_delta="Which option would you like me to take?")])

    LocalHeartbeatBackend().run(api)

    assert api.statuses["worker_pollypm"][0] == "waiting_on_user"
    assert ("worker_pollypm", "needs_followup") not in api.alerts
    assert api.cursor_updates[-1]["verdict"] == "blocked"


def test_local_heartbeat_backend_recovers_missing_window() -> None:
    api = FakeHeartbeatAPI([_context(window_present=False, pane_text="", transcript_delta="")])

    LocalHeartbeatBackend().run(api)

    assert api.statuses["worker_pollypm"][0] == "recovering"
    assert api.recoveries == [("worker_pollypm", "missing_window", "Expected tmux window is missing")]
    assert ("worker_pollypm", "missing_window") in api.alerts


def test_local_heartbeat_backend_rejects_unknown_role_for_mutating_session_writes() -> None:
    backend = LocalHeartbeatBackend()
    api = FakeHeartbeatAPI([])
    context = _context(session_name="mystery", role="mystery")

    with pytest.raises(AssertionError, match="set_session_status"):
        backend._set_session_status(
            api,
            context,
            "recovering",
            reason="Expected tmux window is missing",
        )

    with pytest.raises(AssertionError, match="recover_session"):
        backend._recover_session(
            api,
            context,
            failure_type="missing_window",
            message="Expected tmux window is missing",
        )


def test_local_heartbeat_backend_rejects_non_worker_message_injection() -> None:
    backend = LocalHeartbeatBackend()
    api = FakeHeartbeatAPI([])
    context = _context(session_name="operator", role="operator-pm")

    with pytest.raises(AssertionError, match="send_session_message"):
        backend._send_worker_message(api, context, "pm task next")


def test_local_heartbeat_backend_marks_auth_broken() -> None:
    api = FakeHeartbeatAPI([_context(transcript_delta="Authentication failure: please login again.")])

    LocalHeartbeatBackend().run(api)

    assert api.statuses["worker_pollypm"][0] == "auth_broken"
    assert api.account_marks == [("claude_controller", "claude", "live session reported authentication failure")]
    assert ("worker_pollypm", "auth_broken") in api.alerts


def test_local_heartbeat_backend_raises_alert_for_unfinished_turn() -> None:
    """Heartbeat raises an alert for needs_followup but does NOT inject messages into operator chat."""
    api = FakeHeartbeatAPI([_context(transcript_delta="Implemented the parser. Next step: add coverage.")])

    LocalHeartbeatBackend().run(api)

    # Alert raised but NO message injected into operator chat
    assert api.messages == []
    key = ("worker_pollypm", "needs_followup")
    assert key in api.alerts
    assert "Additional work remains" in api.alerts[key].message


def test_local_heartbeat_backend_sends_persona_drift_remediation_once() -> None:
    context = _context(
        role="operator-pm",
        pane_text="I am Russell and I will handle this.",
        transcript_delta="",
        previous_snapshot_hash="hash-0",
        snapshot_hash="hash-1",
    )
    api = FakeHeartbeatAPI([context])

    LocalHeartbeatBackend().run(api)

    assert ("worker_pollypm", "persona_drift_detected") in api.alerts
    assert len(api.messages) == 1
    session_name, text, owner = api.messages[0]
    assert session_name == "worker_pollypm"
    assert "PollyPM persona-drift correction" in text
    assert owner == "persona-drift-remediation"

    LocalHeartbeatBackend().run(api)

    assert len(api.messages) == 1


def test_local_heartbeat_backend_uses_mechanical_checks_only_for_heartbeat_supervisor() -> None:
    api = FakeHeartbeatAPI(
        [
            _context(
                session_name="heartbeat",
                role="heartbeat-supervisor",
                transcript_delta="Implemented the parser. Next step: add coverage.",
                previous_log_bytes=64,
                previous_snapshot_hash="hash-1",
                snapshot_hash="hash-1",
            )
        ],
        hashes={"heartbeat": ["hash-1", "hash-1", "hash-1"]},
    )

    LocalHeartbeatBackend().run(api)

    assert api.statuses["heartbeat"] == (
        "healthy",
        "Heartbeat supervisor only checks mechanical session health",
    )
    assert ("heartbeat", "needs_followup") not in api.alerts
    assert ("heartbeat", "idle_output") not in api.alerts
    assert ("heartbeat", "suspected_loop") not in api.alerts


def test_local_heartbeat_backend_alerts_on_unmanaged_window_once() -> None:
    api = FakeHeartbeatAPI(
        [_context()],
        unmanaged_windows=[
            HeartbeatUnmanagedWindow(
                tmux_session="pollypm",
                window_name="e2e-sandbox",
                pane_id="%9",
                pane_command="claude",
                pane_dead=False,
                pane_path="/workspace/sandbox",
            )
        ],
    )

    LocalHeartbeatBackend().run(api)

    assert ("heartbeat", "unmanaged_window:pollypm:e2e-sandbox") in api.alerts
    # No chat injection — heartbeat only raises alerts, never messages operator
    assert api.messages == []

    LocalHeartbeatBackend().run(api)

    # Still no messages on second run
    assert api.messages == []


def test_local_heartbeat_backend_clears_stale_unmanaged_window_alerts() -> None:
    api = FakeHeartbeatAPI([_context()])
    api.raise_alert(
        "heartbeat",
        "unmanaged_window:pollypm:e2e-sandbox",
        "warn",
        "Found unmanaged tmux window e2e-sandbox in session pollypm running claude",
    )

    LocalHeartbeatBackend().run(api)

    assert ("heartbeat", "unmanaged_window:pollypm:e2e-sandbox") not in api.alerts


def test_worker_nudge_skips_repeat_for_same_snapshot_episode(monkeypatch) -> None:
    backend = LocalHeartbeatBackend()
    api = FakeHeartbeatAPI(
        [],
        hashes={"worker_pollypm": ["hash-1"] * 7},
    )
    context = _context(
        transcript_delta="",
        pane_text="Still stalled",
        previous_snapshot_hash="hash-1",
        snapshot_hash="hash-1",
    )
    monkeypatch.setattr(backend, "_has_pending_work", lambda *_args: True)
    api.supervisor.msg_store.append_event(
        scope=context.session_name,
        sender=context.session_name,
        subject="nudge",
        payload={"snapshot_hash": "hash-1"},
        created_at=datetime.now(timezone.utc) - timedelta(seconds=601),
    )

    backend._nudge_stalled_worker(api, context)

    assert api.messages == []
    nudge_events = [
        event
        for event in api.supervisor.msg_store.query_messages(
            type="event",
            scope=context.session_name,
            limit=20,
        )
        if event.get("subject") == "nudge"
    ]
    assert len(nudge_events) == 1


def test_worker_nudge_escalates_when_same_episode_keeps_repeating(monkeypatch) -> None:
    backend = LocalHeartbeatBackend()
    api = FakeHeartbeatAPI(
        [],
        hashes={"worker_pollypm": ["hash-1"] * 8},
    )
    context = _context(
        transcript_delta="",
        pane_text="Still stalled",
        previous_snapshot_hash="hash-1",
        snapshot_hash="hash-1",
    )
    monkeypatch.setattr(backend, "_has_pending_work", lambda *_args: True)
    api.supervisor.msg_store.append_event(
        scope=context.session_name,
        sender=context.session_name,
        subject="nudge",
        payload={"snapshot_hash": "hash-1"},
        created_at=datetime.now(timezone.utc) - timedelta(seconds=601),
    )

    backend._nudge_stalled_worker(api, context)

    assert api.messages == []
    assert ("worker_pollypm", "stuck_session") in api.alerts
    escalations = [
        event
        for event in api.supervisor.msg_store.query_messages(
            type="event",
            scope=context.session_name,
            limit=20,
        )
        if event.get("subject") == "nudge_escalated"
    ]
    assert len(escalations) == 1
    assert escalations[0]["payload"]["snapshot_hash"] == "hash-1"


# ---------------------------------------------------------------------------
# #910 — every heartbeat alert routes through SignalEnvelope
# ---------------------------------------------------------------------------


def _capture_routed_envelopes(monkeypatch) -> list:
    """Patch :func:`route_signal` in heartbeats.local so the test
    can read back every envelope routed during a heartbeat sweep."""
    import pollypm.heartbeats.local as hbl
    from pollypm.signal_routing import route_signal as real_route_signal

    captured: list = []

    def _record(envelope):
        captured.append(envelope)
        return real_route_signal(envelope)

    monkeypatch.setattr(hbl, "_route_signal", _record)
    return captured


def test_unmanaged_window_alert_routes_through_signal_envelope(monkeypatch) -> None:
    """#910 — the unmanaged-window alert path constructs and routes
    a SignalEnvelope before the legacy ``raise_alert`` write."""
    from pollypm.signal_routing import SignalActionability

    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [_context()],
        unmanaged_windows=[
            HeartbeatUnmanagedWindow(
                tmux_session="pollypm",
                window_name="e2e-sandbox",
                pane_id="%9",
                pane_command="claude",
                pane_dead=False,
                pane_path="/workspace/sandbox",
            )
        ],
    )

    LocalHeartbeatBackend().run(api)

    types = {env.payload.get("alert_type") if env.payload else None for env in captured}
    sources = {env.source for env in captured}
    dedupe_keys = {env.dedupe_key for env in captured}
    # The unmanaged-window envelope must be present, sourced as
    # heartbeat, and carry a stable dedupe key naming the alert.
    assert "heartbeat" in sources
    matching = [
        env for env in captured
        if env.dedupe_key
        and "unmanaged_window:pollypm:e2e-sandbox" in env.dedupe_key
    ]
    assert matching, (
        "expected an unmanaged_window envelope; got dedupe_keys=%r types=%r"
        % (dedupe_keys, types)
    )
    # Operational unmanaged-window alerts must classify as
    # OPERATIONAL so route_signal sends them to Activity only.
    env = matching[0]
    assert env.actionability is SignalActionability.OPERATIONAL


def test_missing_window_alert_routes_through_signal_envelope(monkeypatch) -> None:
    """#910 — the missing-window path is one of the sites the
    issue specifically called out (line 570). The envelope's
    audience / actionability are derived from
    :func:`alert_channel`; that classifier owns the policy. What
    this test enforces is that the envelope is constructed and
    routed at all (the migration contract)."""
    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [_context(window_present=False, snapshot_path=None)],
    )

    LocalHeartbeatBackend().run(api)

    matching = [
        env for env in captured
        if env.dedupe_key and "missing_window" in env.dedupe_key
    ]
    assert matching, [env.dedupe_key for env in captured]
    env = matching[0]
    assert env.source == "heartbeat"
    assert env.severity.value == "error"
    assert env.suggested_action == "Open Workers and restart the missing session."
    # And the legacy persistence still runs.
    assert ("worker_pollypm", "missing_window") in api.alerts


def test_needs_followup_alert_routes_through_signal_envelope(monkeypatch) -> None:
    """#910 — needs_followup classification (line 813)."""
    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [_context(transcript_delta="Implemented the parser. Next step: add coverage.")]
    )

    LocalHeartbeatBackend().run(api)

    matching = [
        env for env in captured
        if env.dedupe_key and "needs_followup" in env.dedupe_key
    ]
    assert matching, [env.dedupe_key for env in captured]
    assert matching[0].source == "heartbeat"
    # The legacy alert is still persisted for the cockpit reader.
    assert ("worker_pollypm", "needs_followup") in api.alerts


def test_persona_drift_alert_routes_through_signal_envelope(monkeypatch) -> None:
    """#910 — the persona_drift path was the only #894 site that
    routed before, but it persisted the legacy alert *unconditionally*
    alongside the routing call. After consolidation the routing
    funnel is the single source of truth."""
    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [
            _context(
                role="operator-pm",
                pane_text="I am Russell and I will handle this.",
                transcript_delta="",
                previous_snapshot_hash="hash-0",
                snapshot_hash="hash-1",
            )
        ]
    )

    LocalHeartbeatBackend().run(api)

    matching = [
        env for env in captured
        if env.dedupe_key and "persona_drift_detected" in env.dedupe_key
    ]
    assert len(matching) == 1
    env = matching[0]
    assert env.source == "heartbeat"
    from pollypm.signal_routing import (
        SignalActionability,
        SignalAudience,
    )

    # Drift is action-required + user audience → full surface set
    # including Toast (the #894 intent).
    assert env.actionability is SignalActionability.ACTION_REQUIRED
    assert env.audience is SignalAudience.USER
    # And the legacy persistence still happens so existing readers
    # don't regress.
    assert ("worker_pollypm", "persona_drift_detected") in api.alerts


def test_no_heartbeat_alert_skips_signal_envelope_funnel(monkeypatch) -> None:
    """#910 — every alert raised during a sweep must have produced
    at least one SignalEnvelope. Counts must agree."""
    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [
            _context(
                window_present=False,
                snapshot_path=None,
            )
        ]
    )

    LocalHeartbeatBackend().run(api)

    # Every persisted alert (api.alerts is keyed by
    # (session, type)) must have a matching envelope routed.
    persisted_types = {alert_type for (_session, alert_type) in api.alerts}
    routed_types: set[str] = set()
    for env in captured:
        if not env.dedupe_key:
            continue
        # dedupe key shape: "<source>:<alert_type>[:<target>...]"
        parts = env.dedupe_key.split(":")
        if len(parts) >= 2:
            routed_types.add(parts[1])
    # missing_window is the alert raised in this scenario.
    assert "missing_window" in persisted_types
    assert "missing_window" in routed_types


# ---------------------------------------------------------------------------
# #910 follow-up — every heartbeat record_event also routes through
# SignalEnvelope. The activity-feed events (heartbeat_error, sweep
# completion, unmanaged_window) used to bypass the routing policy.
# ---------------------------------------------------------------------------


def test_heartbeat_sweep_completion_event_routes_through_signal_envelope(
    monkeypatch,
) -> None:
    """The sweep-completion ``record_event`` site at the bottom of
    :meth:`LocalHeartbeatBackend.run` must construct + route a
    SignalEnvelope before the legacy persistence write. The envelope
    is operational (Activity-only) by classification — what this
    test enforces is that the funnel runs at all and that the
    legacy persistence still happens for backwards compatibility."""
    from pollypm.signal_routing import (
        SignalActionability,
        SignalAudience,
    )

    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI([_context()])

    LocalHeartbeatBackend().run(api)

    # The sweep completion event is the one keyed on session "heartbeat"
    # with subject "heartbeat".
    matching = [
        env for env in captured
        if env.dedupe_key and "heartbeat:heartbeat" in env.dedupe_key
        and env.payload
        and env.payload.get("event_type") == "heartbeat"
    ]
    assert matching, [
        (env.dedupe_key, dict(env.payload) if env.payload else None)
        for env in captured
    ]
    env = matching[0]
    assert env.source == "heartbeat"
    assert env.audience is SignalAudience.OPERATOR
    assert env.actionability is SignalActionability.OPERATIONAL

    # Legacy persistence still ran via api.record_event.
    persisted_subjects = {event_type for (_session, event_type, _msg) in api.events}
    assert "heartbeat" in persisted_subjects


def test_heartbeat_unmanaged_window_event_routes_through_signal_envelope(
    monkeypatch,
) -> None:
    """The unmanaged-window discovery emits BOTH an alert (already
    routed) AND a record_event activity-feed entry. The follow-up
    fix routes the event through the same funnel pattern."""
    from pollypm.signal_routing import SignalActionability

    captured = _capture_routed_envelopes(monkeypatch)
    api = FakeHeartbeatAPI(
        [_context()],
        unmanaged_windows=[
            HeartbeatUnmanagedWindow(
                tmux_session="pollypm",
                window_name="rogue-window",
                pane_id="%99",
                pane_command="claude",
                pane_dead=False,
                pane_path="/tmp/rogue",
            )
        ],
    )

    LocalHeartbeatBackend().run(api)

    matching = [
        env for env in captured
        if env.payload
        and env.payload.get("event_type") == "unmanaged_window"
    ]
    assert matching, [
        dict(env.payload) if env.payload else None for env in captured
    ]
    env = matching[0]
    assert env.source == "heartbeat"
    assert env.actionability is SignalActionability.OPERATIONAL

    # Legacy persistence path still records the event.
    persisted_subjects = {event_type for (_session, event_type, _msg) in api.events}
    assert "unmanaged_window" in persisted_subjects

