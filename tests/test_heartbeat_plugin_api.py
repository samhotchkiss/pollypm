from __future__ import annotations

import subprocess
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
    monkeypatch.setattr(supervisor, "window_map", lambda: {launch.window_name: window})
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
    monkeypatch.setattr(
        supervisor,
        "window_map",
        lambda: {
            managed_window.name: managed_window,
            console_window.name: console_window,
            unmanaged_window.name: unmanaged_window,
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
