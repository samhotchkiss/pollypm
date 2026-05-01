import base64
import json
import shlex
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _decode_launch_payload(command: str) -> dict:
    """Extract and decode the base64 runtime_launcher payload from a launch command."""
    # Command may be wrapped in sh -lc '...'
    parts = shlex.split(command)
    if parts[0] == "sh" and "-lc" in parts:
        inner = parts[-1]
        parts = shlex.split(inner)
    payload = parts[-1]
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return json.loads(raw.decode("utf-8"))

from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
    SessionLaunchSpec,
)
from pollypm.supervisor import (
    Supervisor,
    _extract_claude_model_name,
    _extract_codex_model_name,
    _extract_token_metrics,
)
from pollypm.tmux.client import TmuxPane, TmuxWindow


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
            failover_enabled=True,
            failover_accounts=["codex_backup"],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm/homes/codex_backup",
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
            )
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


def test_supervisor_failover_prefers_viable_backup(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "operator")
    restarted: dict[str, str] = {}

    monkeypatch.setattr(supervisor, "_account_is_viable", lambda name: name == "codex_backup")
    monkeypatch.setattr(
        supervisor,
        "_restart_session",
        lambda session_name, account_name, failure_type: restarted.update(
            {"session": session_name, "account": account_name, "failure": failure_type}
        ),
    )

    supervisor._maybe_recover_session(launch, failure_type="auth_broken", failure_message="401")

    assert restarted == {
        "session": "operator",
        "account": "codex_backup",
        "failure": "auth_broken",
    }


def test_ensure_layout_skips_scaffolding_global_control_root(
    monkeypatch, tmp_path: Path,
) -> None:
    config_root = tmp_path / ".pollypm"
    config = _config(config_root)
    real_project = tmp_path / "demo"
    config.projects = {
        "demo": KnownProject(
            key="demo",
            path=real_project,
            name="Demo",
            kind=ProjectKind.FOLDER,
        ),
    }
    supervisor = Supervisor(config)
    scaffolded: list[Path] = []

    monkeypatch.setattr(
        "pollypm.supervisor.ensure_project_scaffold",
        lambda path: (scaffolded.append(Path(path)), Path(path))[1],
    )

    supervisor.ensure_layout()

    assert config.project.root_dir not in scaffolded
    assert real_project in scaffolded


def test_human_input_creates_automatic_lease(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    monkeypatch.setattr(supervisor.tmux, "send_keys", lambda *args, **kwargs: None)

    supervisor.send_input("operator", "hello", owner="human")

    lease = supervisor.store.get_lease("operator")
    assert lease is not None
    assert lease.owner == "human"


def test_send_input_prefixes_heartbeat_messages(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor.tmux, "send_keys", lambda target, text, **kw: sent.append((target, text)))

    supervisor.send_input("operator", "check session", owner="heartbeat")
    assert len(sent) == 1
    assert sent[0][1] == "H: check session"


def test_send_input_prefixes_polly_messages(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor.tmux, "send_keys", lambda target, text, **kw: sent.append((target, text)))

    supervisor.send_input("operator", "do this task", owner="polly")
    assert len(sent) == 1
    assert sent[0][1] == "P: do this task"


def test_send_input_no_prefix_for_human(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor.tmux, "send_keys", lambda target, text, **kw: sent.append((target, text)))

    supervisor.send_input("operator", "hello", owner="human")
    assert len(sent) == 1
    assert sent[0][1] == "hello"


def test_send_input_sends_extra_enter_for_codex(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        window_name="pm-operator",
    )
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    sent: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        supervisor.tmux,
        "send_keys",
        lambda target, text, press_enter=True: sent.append((target, text, press_enter)),
    )

    supervisor.send_input("operator", "continue", owner="human")

    assert len(sent) == 2
    assert sent[0][1:] == ("continue", True)
    assert sent[1][1:] == ("", True)
    assert sent[0][0] == sent[1][0]
    assert sent[0][0].endswith(":pm-operator")


def test_send_input_targets_mounted_cockpit_pane_when_window_not_in_storage(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    state_path = config.project.base_dir / "cockpit_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"mounted_session": "operator", "right_pane_id": "%42"}))

    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: True)
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda name: [
            TmuxWindow(
                session=name,
                index=0,
                name="pm-heartbeat",
                active=True,
                pane_id="%10",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
        ],
    )
    # Mock list_panes to validate the cockpit pane exists
    monkeypatch.setattr(
        supervisor.tmux,
        "list_panes",
        lambda target: [
            TmuxPane(
                session="pollypm",
                window_index=0,
                window_name="PollyPM",
                pane_index=1,
                pane_id="%42",
                active=True,
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
                pane_left=0,
                pane_width=80,
            )
        ],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor.tmux, "send_keys", lambda target, text, **kw: sent.append((target, text)))

    supervisor.send_input("operator", "hello", owner="human")

    assert sent == [("%42", "hello")]


def test_send_input_raises_when_session_not_found(monkeypatch, tmp_path: Path) -> None:
    """send_input raises RuntimeError when the target session doesn't exist anywhere."""
    import pytest
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: True)
    monkeypatch.setattr(supervisor.tmux, "list_windows", lambda name: [])

    with pytest.raises(RuntimeError, match="not found"):
        supervisor.send_input("operator", "hello", owner="human")


# ---------------------------------------------------------------------------
# #924 — pm send must reach per-task workers (task-<project>-<N> windows)
# ---------------------------------------------------------------------------


def test_send_input_resolves_per_task_worker_window(monkeypatch, tmp_path: Path) -> None:
    """``pm send task-<project>-<N>`` reaches the per-task pane.

    Per #919 / #921, per-task workers live in the storage closet as
    ``task-<project>-<N>`` windows that are *not* members of the launch
    plan. Pre-#924 ``launch_by_session`` raised ``KeyError`` and the
    only documented mid-flow steering CLI was unreachable.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    storage_closet = supervisor.storage_closet_session_name()

    # Make the storage closet present and serve the task window.
    monkeypatch.setattr(
        supervisor.tmux, "has_session", lambda name: name == storage_closet,
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda name: [
            TmuxWindow(
                session=name,
                index=3,
                name="task-blackjack-trainer-6",
                active=False,
                pane_id="%99",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
        ],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )

    supervisor.send_input(
        "task-blackjack-trainer-6", "use the new helper", owner="human",
    )

    assert sent == [
        (f"{storage_closet}:task-blackjack-trainer-6", "use the new helper"),
    ]


def test_send_input_for_config_defined_session_still_works(
    monkeypatch, tmp_path: Path,
) -> None:
    """Regression guard — config-defined names keep resolving via the plan.

    The #924 fallback must only kick in for ``task-<project>-<N>`` names
    that the static plan does not cover. Config-defined sessions
    (operator, heartbeat, ...) still go through ``plan_launches``.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    storage_closet = supervisor.storage_closet_session_name()

    monkeypatch.setattr(
        supervisor.tmux, "has_session", lambda name: name == storage_closet,
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda name: [
            TmuxWindow(
                session=name,
                index=0,
                name="pm-operator",
                active=True,
                pane_id="%10",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
        ],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )

    supervisor.send_input("operator", "hello", owner="human")

    assert sent == [(f"{storage_closet}:pm-operator", "hello")]


def test_launch_by_session_unknown_name_raises_friendly_keyerror(
    tmp_path: Path,
) -> None:
    """Unknown bogus names get a friendlier message that points at next steps.

    Pre-#924 the bare ``KeyError: 'Unknown session: <name>'`` left the
    user with no path forward; the new message lists configured sessions
    and points at ``pm task next`` for per-task workers.
    """
    import pytest

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    with pytest.raises(KeyError) as excinfo:
        supervisor.launch_by_session("totally-bogus-name")
    msg = str(excinfo.value)
    assert "Unknown session: totally-bogus-name" in msg
    # Lists at least one of the configured sessions.
    assert "operator" in msg or "heartbeat" in msg
    # Points at the per-task discovery path.
    assert "pm task next" in msg


def test_launch_by_session_synthesizes_per_task_spec(tmp_path: Path) -> None:
    """The planner returns a synthesized spec for ``task-<project>-<N>``.

    The spec must carry enough metadata for ``send_input`` to operate:
    a ``window_name`` matching the task window, a session whose
    ``provider`` is set (so the Codex extra-Enter check is meaningful),
    and a project key parsed from the name.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    spec = supervisor.launch_by_session("task-blackjack-trainer-6")
    assert spec.window_name == "task-blackjack-trainer-6"
    assert spec.session.name == "task-blackjack-trainer-6"
    assert spec.session.project == "blackjack-trainer"
    # Provider must resolve to a real ProviderKind so the codex
    # extra-enter branch in send_input behaves correctly.
    assert spec.session.provider in (ProviderKind.CLAUDE, ProviderKind.CODEX)


def test_claim_lease_rejects_conflicting_owner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")

    try:
        supervisor.claim_lease("operator", "pm-bot", "override")
    except RuntimeError as exc:
        assert "currently leased to human" in str(exc)
    else:
        raise AssertionError("expected conflicting lease claim to fail")


def test_release_lease_clears_active_lease_and_records_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")

    supervisor.release_lease("operator", expected_owner="human")

    assert supervisor.store.get_lease("operator") is None
    # #349: record_event now writes the audit row to the unified
    # ``messages`` table. Lease transitions use the synchronous Store
    # path so this query sees the row immediately.
    events = supervisor.msg_store.query_messages(
        type="event", scope="operator", limit=5,
    )
    assert any(
        event["scope"] == "operator"
        and event["subject"] == "lease"
        and event.get("payload", {}).get("message") == "Lease released"
        for event in events
    )


def test_release_lease_preserves_reclaimed_lease_for_different_owner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")

    supervisor.release_lease("operator", expected_owner="polly")

    lease = supervisor.store.get_lease("operator")
    assert lease is not None
    assert lease.owner == "human"


def test_release_expired_leases_clears_stale_lease_and_records_event(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.pollypm.lease_timeout_minutes = 30
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")
    expired_at = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    supervisor.store.execute(
        "UPDATE leases SET updated_at = ? WHERE session_name = ?",
        (expired_at, "operator"),
    )
    supervisor.store.commit()

    released = supervisor.release_expired_leases(now=datetime.now(UTC))

    assert [lease.session_name for lease in released] == ["operator"]
    assert supervisor.store.get_lease("operator") is None
    # #349: lease transitions land in ``messages`` via the sync Store path.
    events = supervisor.msg_store.query_messages(
        type="event", scope="operator", limit=5,
    )
    assert any(
        event["scope"] == "operator"
        and event["subject"] == "lease"
        and "Auto-released expired lease held by human" in (
            event.get("payload", {}).get("message") or ""
        )
        for event in events
    )


def test_release_expired_leases_pluralises_minute_in_message(tmp_path: Path) -> None:
    """Cycle 110 — the auto-release event message hard-pluralised
    ``minutes``, so a 1-minute lease timeout produced ``after 1
    minutes``. Match the noun to the count."""
    config = _config(tmp_path)
    config.pollypm.lease_timeout_minutes = 1
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")
    expired_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    supervisor.store.execute(
        "UPDATE leases SET updated_at = ? WHERE session_name = ?",
        (expired_at, "operator"),
    )
    supervisor.store.commit()
    supervisor.release_expired_leases(now=datetime.now(UTC))
    events = supervisor.msg_store.query_messages(
        type="event", scope="operator", limit=5,
    )
    messages = [
        e.get("payload", {}).get("message") or "" for e in events
    ]
    assert any("after 1 minute" in m and "1 minutes" not in m for m in messages), messages


def test_release_expired_leases_is_available_for_alerts_gc_handler(monkeypatch, tmp_path: Path) -> None:
    """Lease release is now the ``alerts.gc`` recurring handler's job
    (migrated from inline Phase 1 dispatch by #184). Verify the
    supervisor-side API is still intact so the handler can call it.
    """
    config = _config(tmp_path)
    config.pollypm.lease_timeout_minutes = 30
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")
    expired_at = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    supervisor.store.execute(
        "UPDATE leases SET updated_at = ? WHERE session_name = ?",
        (expired_at, "operator"),
    )
    supervisor.store.commit()

    released = supervisor.release_expired_leases()
    assert [lease.session_name for lease in released] == ["operator"]
    assert supervisor.store.get_lease("operator") is None


def test_heartbeat_uses_separate_tmux_session(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    created_sessions: list[tuple[str, str, str]] = []
    created_windows: list[tuple[str, str, str]] = []
    piped_targets: list[str] = []
    window_options: list[tuple[str, str, str]] = []

    monkeypatch.setattr(supervisor, "_probe_controller_account", lambda account_name: None)
    monkeypatch.setattr(supervisor, "_stabilize_launch", lambda launch, target, on_status=None: None)
    monkeypatch.setattr(supervisor, "_stabilize_claude_launch", lambda target: None)
    monkeypatch.setattr(supervisor, "_stabilize_codex_launch", lambda target: None)
    monkeypatch.setattr(supervisor, "_send_initial_input_if_fresh", lambda launch, target: None)
    monkeypatch.setattr(supervisor, "_record_launch", lambda launch: None)
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: False)
    monkeypatch.setattr(
        supervisor.tmux,
        "create_session",
        lambda name, window_name, command, **kwargs: created_sessions.append((name, window_name, command)),
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: created_windows.append((name, window_name, command, detached)),
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "set_window_option",
        lambda target, option, value: window_options.append((target, option, value)),
    )
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: piped_targets.append(target))
    monkeypatch.setattr(supervisor, "ensure_console_window", lambda: None)
    monkeypatch.setattr(supervisor, "focus_console", lambda: None)

    controller = supervisor.bootstrap_tmux()

    assert controller == "claude_controller"
    assert created_sessions[0][0] == supervisor.storage_closet_session_name()
    assert created_sessions[0][1] == "pm-heartbeat"
    assert created_sessions[1][0] == config.project.tmux_session
    assert created_sessions[1][1] == supervisor.console_window_name()
    assert piped_targets == [f"{supervisor.storage_closet_session_name()}:pm-heartbeat", f"{supervisor.storage_closet_session_name()}:pm-operator"]
    assert (f"{supervisor.storage_closet_session_name()}:pm-heartbeat", "focus-events", "on") in window_options
    assert (f"{supervisor.storage_closet_session_name()}:pm-operator", "focus-events", "on") in window_options
    assert (f"{config.project.tmux_session}:{supervisor.console_window_name()}", "focus-events", "on") in window_options


def test_bootstrap_returns_before_provider_stabilization_finishes(
    monkeypatch, tmp_path: Path,
) -> None:
    """First launch should attach to the cockpit without waiting on agents."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    created_sessions: list[tuple[str, str, str]] = []
    created_windows: list[tuple[str, str, str, bool]] = []
    release_stabilizer = threading.Event()
    stabilizer_started = threading.Event()
    bootstrap_done = threading.Event()
    sent_initial: list[str] = []
    result: dict[str, str] = {}

    monkeypatch.setattr(supervisor, "_probe_controller_account", lambda account_name: None)
    monkeypatch.setattr(supervisor, "_record_launch", lambda launch: None)
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: False)
    monkeypatch.setattr(
        supervisor.tmux,
        "create_session",
        lambda name, window_name, command, **kwargs: (
            created_sessions.append((name, window_name, command)) or f"%{len(created_sessions)}"
        ),
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: (
            created_windows.append((name, window_name, command, detached))
            or f"%w{len(created_windows)}"
        ),
    )
    monkeypatch.setattr(supervisor.tmux, "set_window_option", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor, "ensure_console_window", lambda: None)
    monkeypatch.setattr(supervisor, "focus_console", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_send_initial_input_if_fresh",
        lambda launch, target: sent_initial.append(launch.session.name),
    )

    def _slow_stabilize(_target: str) -> None:
        stabilizer_started.set()
        release_stabilizer.wait(timeout=5)

    monkeypatch.setattr(supervisor, "_stabilize_claude_launch", _slow_stabilize)
    monkeypatch.setattr(supervisor, "_stabilize_codex_launch", _slow_stabilize)

    def _run_bootstrap() -> None:
        result["controller"] = supervisor.bootstrap_tmux()
        bootstrap_done.set()

    thread = threading.Thread(target=_run_bootstrap)
    thread.start()
    try:
        assert bootstrap_done.wait(timeout=0.5), (
            "bootstrap_tmux blocked on provider stabilization before cockpit attach"
        )
        assert stabilizer_started.wait(timeout=1)
        assert sent_initial == []
    finally:
        release_stabilizer.set()
        thread.join(timeout=2)
        coordinator = getattr(supervisor, "_bootstrap_completion_thread", None)
        if coordinator is not None:
            coordinator.join(timeout=2)

    assert result["controller"] == "claude_controller"
    assert created_sessions[0][0] == supervisor.storage_closet_session_name()
    assert created_sessions[1][0] == config.project.tmux_session
    assert set(sent_initial) == {"heartbeat", "operator"}


def test_launch_session_creates_worker_window_detached(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="Inspect the repo",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)

    created_windows: list[tuple[str, str, str, bool]] = []
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda _name: True)
    monkeypatch.setattr(supervisor, "_window_map", lambda: {})
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: created_windows.append((name, window_name, command, detached)),
    )
    monkeypatch.setattr(supervisor.tmux, "set_window_option", lambda target, option, value: None)
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: None)
    monkeypatch.setattr(supervisor, "_record_launch", lambda launch: None)
    monkeypatch.setattr(supervisor, "_stabilize_launch", lambda launch, target, on_status=None: None)

    supervisor.launch_session("worker")

    assert len(created_windows) == 1
    assert created_windows[0][0] == supervisor.storage_closet_session_name()
    assert created_windows[0][1] == "worker-pollypm"
    assert created_windows[0][3] is True


def test_write_snapshot_targets_pane_id_for_mounted_windows(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    captured: dict[str, object] = {}

    def fake_capture_pane(target: str, lines: int = 200) -> str:
        captured["target"] = target
        captured["lines"] = lines
        return "snapshot\n"

    monkeypatch.setattr(supervisor.tmux, "capture_pane", fake_capture_pane)
    window = TmuxWindow(
        session="pollypm",
        index=0,
        name="worker-pollypm",
        active=True,
        pane_id="%42",
        pane_current_command="node",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )

    snapshot_path, content = supervisor._write_snapshot(window, 180)

    assert captured == {"target": "%42", "lines": 180}
    assert content == "snapshot\n"
    assert snapshot_path.exists()


def test_control_session_args_follow_override_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["heartbeat"].args = ["--dangerously-skip-permissions"]
    config.sessions["operator"].args = ["--dangerously-skip-permissions"]
    supervisor = Supervisor(config)

    launches = {launch.session.name: launch for launch in supervisor.plan_launches(controller_account="codex_backup")}

    assert launches["heartbeat"].session.provider is ProviderKind.CODEX
    assert launches["heartbeat"].session.args == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
    ]
    assert launches["operator"].session.provider is ProviderKind.CODEX
    assert launches["operator"].session.args == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4",
    ]


def test_claude_control_sessions_use_original_account_home(tmp_path: Path) -> None:
    """Claude control sessions must use the original account home so macOS Keychain auth is preserved."""
    config = _config(tmp_path)
    source_home = config.accounts["claude_controller"].home
    assert source_home is not None
    (source_home / ".claude").mkdir(parents=True, exist_ok=True)
    (source_home / ".claude" / "settings.json").write_text('{"theme":"dark"}\n')
    (source_home / ".claude.json").write_text('{"hasCompletedOnboarding": true}\n')

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    heartbeat_home = launches["heartbeat"].account.home
    operator_home = launches["operator"].account.home

    # Claude accounts keep their original home for Keychain auth
    assert heartbeat_home == source_home
    assert operator_home == source_home


def test_create_session_window_records_claude_resume_ids(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    source_home = config.accounts["claude_controller"].home
    assert source_home is not None
    (source_home / ".claude").mkdir(parents=True, exist_ok=True)
    (source_home / ".claude" / "settings.json").write_text("{}\n")
    (source_home / ".claude.json").write_text('{"hasCompletedOnboarding": true}\n')

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    bucket = source_home / ".claude" / "projects" / str(tmp_path.resolve()).replace("/", "-")
    created_tmux_sessions: set[str] = set()

    def _write_transcript(session_name: str) -> None:
        # #935 — write a first user message that references the
        # session's control-prompts file so the capture-time validator
        # can prove the transcript belongs to this launch (control
        # sessions sharing a Claude bucket otherwise get
        # cross-attributed; see issue #935).
        bucket.mkdir(parents=True, exist_ok=True)
        session_id = f"{session_name}-uuid"
        bootstrap = (
            "[PollyPM bootstrap]\rRead "
            f"/x/.pollypm/control-prompts/{session_name}.md, "
            'adopt it as your operating instructions.'
        )
        records = [
            {"sessionId": session_id, "type": "permission-mode"},
            {
                "type": "user",
                "message": {"role": "user", "content": bootstrap},
                "sessionId": session_id,
            },
        ]
        (bucket / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    def _create_session(tmux_session: str, window_name: str, command: str) -> str:
        del command
        created_tmux_sessions.add(tmux_session)
        _write_transcript("heartbeat")
        return "%1"

    def _create_window(tmux_session: str, window_name: str, command: str, detached: bool = True) -> str:
        del tmux_session, command, detached
        _write_transcript("operator")
        return "%2"

    monkeypatch.setattr(supervisor, "_window_map", lambda: {})
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: name in created_tmux_sessions)
    monkeypatch.setattr(supervisor.tmux, "create_session", _create_session)
    monkeypatch.setattr(supervisor.tmux, "create_window", _create_window)
    monkeypatch.setattr(supervisor.tmux, "set_window_option", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.tmux, "set_pane_history_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda *args, **kwargs: None)

    heartbeat_launch, heartbeat_target = supervisor.create_session_window("heartbeat")
    operator_launch, operator_target = supervisor.create_session_window("operator")

    # #934 — ``create_session_window`` now returns the pane_id as the
    # target (when ``create_session``/``create_window`` returns one) so
    # downstream stabilize/kickoff sends address a stable pane id rather
    # than a window-name target that can resolve through tmux to a
    # different pane after a join_pane/restart race. The stub returns
    # ``%1`` from ``create_session`` and ``%2`` from ``create_window``.
    assert heartbeat_target == "%1"
    assert operator_target == "%2"
    assert heartbeat_launch.resume_marker is not None
    assert operator_launch.resume_marker is not None
    # #935 — the resume-UUID capture moved out of
    # ``create_session_window`` and into ``_stabilize_launch`` so it
    # runs AFTER the bootstrap lands in the new transcript (the only
    # durable signal that disambiguates two control sessions sharing
    # one Claude transcript bucket). The pre-launch transcript
    # snapshot is stashed on the supervisor for the post-bootstrap
    # capture to consume; firing the capture here directly exercises
    # the capture path against the fixture transcripts written above.
    assert "heartbeat" in supervisor._pre_launch_claude_ids
    assert "operator" in supervisor._pre_launch_claude_ids
    supervisor._capture_claude_resume_session_id(
        heartbeat_launch,
        previous_ids=supervisor._pre_launch_claude_ids.pop("heartbeat"),
    )
    supervisor._capture_claude_resume_session_id(
        operator_launch,
        previous_ids=supervisor._pre_launch_claude_ids.pop("operator"),
    )
    assert heartbeat_launch.resume_marker.read_text(encoding="utf-8").strip() == "heartbeat-uuid"
    assert operator_launch.resume_marker.read_text(encoding="utf-8").strip() == "operator-uuid"


def test_codex_control_home_syncs_global_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.accounts["codex_backup"] = AccountConfig(
        name="codex_backup",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        home=tmp_path / ".pollypm" / "homes" / "codex_backup",
    )
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="watch the project",
        window_name="pm-operator",
    )
    source_home = config.accounts["codex_backup"].home
    assert source_home is not None
    (source_home / ".codex").mkdir(parents=True, exist_ok=True)
    (source_home / ".codex" / "auth.json").write_text('{"token":"base"}\n')
    (source_home / ".codex" / "config.toml").write_text('model = "gpt-5.4"\n')
    (source_home / ".codex" / ".codex-global-state.json").write_text('{"thread-horizontal:split-left-width":0.5}\n')

    supervisor = Supervisor(config)
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    operator_home = launches["operator"].account.home
    assert operator_home is not None

    assert (operator_home / ".codex" / ".codex-global-state.json").exists()


def test_control_sessions_use_agent_profiles_for_prompts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert "Polly" in launches["operator"].session.prompt
    assert "heartbeat supervisor" in launches["heartbeat"].session.prompt


def test_architect_launch_prefers_project_local_guide(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["architect"] = SessionConfig(
        name="architect",
        role="architect",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="pollypm",
        window_name="architect-pollypm",
    )
    guide_path = tmp_path / ".pollypm" / "project-guides" / "architect.md"
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text("---\nforked_from: test-sha\n---\n\n# Custom architect guide\n")

    supervisor = Supervisor(config)
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert launches["architect"].session.prompt == "# Custom architect guide"


def test_reviewer_launch_prefers_project_local_guide(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["reviewer"] = SessionConfig(
        name="reviewer",
        role="reviewer",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="pollypm",
        window_name="reviewer-pollypm",
    )
    guide_path = tmp_path / ".pollypm" / "project-guides" / "reviewer.md"
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text("---\nforked_from: test-sha\n---\n\n# Custom reviewer guide\n")

    supervisor = Supervisor(config)
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert "# Custom reviewer guide" in launches["reviewer"].session.prompt


def test_open_permissions_default_can_disable_launch_args(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.pollypm.open_permissions_by_default = False
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="Inspect the repo",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    # Role restrictions apply even without open-permissions defaults.
    assert "--allowedTools" in launches["heartbeat"].session.args
    assert "--disallowedTools" in launches["heartbeat"].session.args
    assert "--allowedTools" in launches["operator"].session.args
    assert "--disallowedTools" in launches["operator"].session.args
    assert "--dangerously-skip-permissions" not in launches["heartbeat"].session.args
    assert "--dangerously-skip-permissions" not in launches["operator"].session.args
    assert launches["worker"].session.args == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4",
    ]


def test_run_heartbeat_delegates_to_configured_backend(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    called: dict[str, int] = {}

    class FakeHeartbeatBackend:
        name = "fake"

        def run(self, _supervisor, *, snapshot_lines=200):
            called["snapshot_lines"] = snapshot_lines
            return []

    monkeypatch.setattr(
        "pollypm.supervisor.get_heartbeat_backend",
        lambda name, root_dir=None: FakeHeartbeatBackend(),
    )

    supervisor.run_heartbeat(snapshot_lines=77)

    assert called == {"snapshot_lines": 77}


def test_codex_stabilizer_accepts_mixed_case_banner(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    panes = [
        """
╭───────────────────────────────────────╮
│ >_ OpenAI Codex (v0.118.0)            │
│ model:     gpt-5.4                    │
╰───────────────────────────────────────╯

› Ready
""".strip()
    ]
    monkeypatch.setattr(supervisor.tmux, "capture_pane", lambda target, lines=260: panes[0])
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    supervisor._stabilize_codex_launch("pollypm:0")


def test_codex_stabilizer_accepts_active_working_state(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    pane = """
╭───────────────────────────────────────╮
│ >_ OpenAI Codex (v0.118.0)            │
│ model:     gpt-5.4                    │
╰───────────────────────────────────────╯

• Working (12s • esc to interrupt)

› Implement {feature}
""".strip()
    monkeypatch.setattr(supervisor.tmux, "capture_pane", lambda target, lines=260: pane)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    supervisor._stabilize_codex_launch("pollypm:0")


def test_codex_control_launches_use_agents_md_instead_of_visible_prompt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["operator"] = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="x" * 400,
        window_name="pm-operator",
    )
    supervisor = Supervisor(config)

    launch = next(item for item in supervisor.plan_launches() if item.session.name == "operator")

    decoded = _decode_launch_payload(launch.command)
    argv = decoded["argv"]
    env = decoded["env"]

    # #965 — argv[0] is resolved to an absolute path so the launcher's
    # ``os.execvpe`` does not search a sanitized child PATH. The
    # binary's basename is still ``codex``; if shutil.which finds no
    # codex on this machine, argv[0] stays as the bare name.
    assert Path(argv[0]).name == "codex"
    assert argv[1:] == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4",
    ]
    # #1011 — the profile prompt is no longer round-tripped through
    # ``PM_CODEX_HOME_AGENTS_MD`` (which exploded the wrapped argv past
    # tmux's ``respawn-pane`` command-buffer limit for ~20KB reviewer
    # prompts). The planner now writes ``AGENTS.md`` directly to the
    # session's effective codex_home (the per-session control-home, see
    # ``ControlHomes.effective_account``) before the launch fires.
    assert "PM_CODEX_HOME_AGENTS_MD" not in env
    assert launch.initial_input is None
    agents_md = launch.account.home / ".codex" / "AGENTS.md"
    assert agents_md.exists(), f"expected AGENTS.md at {agents_md}"
    contents = agents_md.read_text()
    assert "Polly" in contents


def test_codex_control_launches_keep_wrapped_argv_below_tmux_command_limit(
    tmp_path: Path,
) -> None:
    """Regression for #1011 — the wrapped launch argv must stay short
    enough that tmux ``respawn-pane`` accepts it.

    The auto-recovery path (#1005) for ``reviewer/no_session`` calls
    ``pm worker-start --role reviewer <project>``, which builds a
    Codex control launch carrying the full Russell reviewer profile
    prompt (~20KB). Pre-fix the planner stuffed that prompt into
    ``PM_CODEX_HOME_AGENTS_MD`` for the runtime launcher to materialise
    on disk; the resulting base64 payload made the wrapped ``sh -lc
    'exec ... <payload>'`` argv exceed tmux's ~17KB ``respawn-pane``
    command-buffer limit (tmux 3.6: ``command too long``), so the
    spawn silently failed and left the pane at zsh.

    Cap-budget the wrapped command at 14KB — comfortably under tmux's
    limit on every release we care about — and require the AGENTS.md
    payload survives via the on-disk file path instead.
    """
    config = _config(tmp_path)
    huge_prompt = "Russell the reviewer\n" + ("verbose criterion checklist\n" * 600)
    assert len(huge_prompt) > 16000  # well past tmux's command-buffer limit
    # Use ``triage`` because it's a control role (so the env-var-vs-
    # disk packaging branch fires) but the planner doesn't replace
    # the session prompt with a shipped profile prompt for it. That
    # means the test's ``huge_prompt`` actually drives the launch
    # payload size.
    config.sessions["triage"] = SessionConfig(
        name="triage",
        role="triage",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt=huge_prompt,
        window_name="pm-triage",
    )
    supervisor = Supervisor(config)

    launches = list(supervisor.plan_launches())
    launch = next(item for item in launches if item.session.name == "triage")

    assert len(launch.command) < 14000, (
        f"wrapped launch command is {len(launch.command)} bytes — tmux "
        "respawn-pane will reject it as 'command too long'"
    )
    decoded = _decode_launch_payload(launch.command)
    assert "PM_CODEX_HOME_AGENTS_MD" not in decoded["env"]
    agents_md = launch.account.home / ".codex" / "AGENTS.md"
    assert agents_md.exists()
    contents = agents_md.read_text()
    # Prompt content lands on disk regardless of whether the planner
    # substituted a shipped profile prompt or kept the session's own.
    assert (
        "Russell the reviewer" in contents
        or "verbose criterion checklist" in contents
        or "Polly" in contents
    )


def test_codex_worker_launches_do_not_auto_send_prompt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="do some work",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)

    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    decoded = _decode_launch_payload(launch.command)

    # Worker prompt should NOT be baked into argv
    argv = decoded["argv"]
    assert not any("do some work" in arg for arg in argv)
    # #965 — argv[0] is resolved to an absolute path before launcher
    # serialization; basename is still ``codex``.
    assert Path(argv[0]).name == "codex"
    assert argv[1:] == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4",
    ]
    shim_path = Path(decoded["env"]["PATH"].split(":")[0])
    assert shim_path.name == "worker"
    assert (shim_path / "tmux").exists()
    assert (shim_path / "pm").exists()


def test_worker_launch_env_blocks_tmux_and_session_pm_commands(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="do some work",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)

    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    decoded = _decode_launch_payload(launch.command)
    shim_dir = Path(decoded["env"]["PATH"].split(":")[0])

    tmux_text = (shim_dir / "tmux").read_text()
    pm_text = (shim_dir / "pm").read_text()

    assert "may not manage tmux directly" in tmux_text
    assert "pm send" in pm_text
    assert "pm console" in pm_text


def test_switch_session_account_restarts_in_place(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    restarted: dict[str, str] = {}

    monkeypatch.setattr(
        supervisor,
        "_restart_session",
        lambda session_name, account_name, failure_type: restarted.update(
            {"session": session_name, "account": account_name, "failure": failure_type}
        ),
    )
    supervisor.switch_session_account("operator", "codex_backup")

    assert restarted == {
        "session": "operator",
        "account": "codex_backup",
        "failure": "manual_switch",
    }
    runtime = supervisor.store.get_session_runtime("operator")
    assert runtime is not None
    assert runtime.effective_account == "codex_backup"


def test_account_is_viable_for_claude_when_credentials_file_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    credentials_path = config.accounts["claude_controller"].home / ".claude" / ".credentials.json"
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text("{}")

    assert supervisor._account_is_viable("claude_controller") is True


def test_account_is_viable_rejects_runtime_marked_exhausted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    auth_path = config.accounts["codex_backup"].home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("{}")
    supervisor.store.upsert_account_runtime(
        account_name="codex_backup",
        provider="codex",
        status="exhausted",
        reason="usage cap reached",
    )

    assert supervisor._account_is_viable("codex_backup") is False


def test_extract_claude_token_metrics() -> None:
    pane = """
           Claude Code v2.1.96
 ▐▛███▜▌   Opus 4.6 (1M context) · Claude Max
...
  ⏵⏵ bypass permissions on (shift+tab to cycle)                                                                                                         22038 tokens
"""
    assert _extract_claude_model_name(pane) == "Opus 4.6 (1M context)"
    assert _extract_token_metrics(ProviderKind.CLAUDE, pane) == ("Opus 4.6 (1M context)", 22038)


def test_extract_codex_model_name() -> None:
    pane = """
OpenAI Codex

› Implement {feature}

  gpt-5.4 high · 30% left · ~/dev/otter-camp
"""
    assert _extract_codex_model_name(pane) == "gpt-5.4 high"


def test_stop_session_rejects_conflicting_lease(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda _name: True)

    try:
        supervisor.stop_session("operator")
    except RuntimeError as exc:
        assert "currently leased to human" in str(exc)
    else:
        raise AssertionError("expected stop to fail while human lease is active")


def test_switch_session_account_rejects_conflicting_lease(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.claim_lease("operator", "human", "manual takeover")

    try:
        supervisor.switch_session_account("operator", "codex_backup")
    except RuntimeError as exc:
        assert "currently leased to human" in str(exc)
    else:
        raise AssertionError("expected account switch to fail while human lease is active")


def test_recovery_waits_on_non_pollypm_lease(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "operator")
    supervisor.claim_lease("operator", "human", "manual takeover")

    supervisor._maybe_recover_session(
        launch,
        failure_type="auth_broken",
        failure_message="401",
    )

    alerts = supervisor.open_alerts()
    assert len(alerts) == 1
    assert alerts[0].alert_type == "recovery_waiting_on_human"
    assert "lease owner human" in alerts[0].message


def test_stalled_worker_gets_heartbeat_nudge_after_five_identical_cycles(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    # #1004: pin workspace_root so resolve_work_db_path lands inside
    # tmp_path rather than the real ~/dev — the test asserts the
    # *stall* nudge fires, which depends on _build_task_nudge finding
    # no queued work for this worker. With workspace_root unset (the
    # default), the resolver falls back to the developer's real
    # ~/dev/.pollypm/state.db, which can carry a queued ``pollypm/1``
    # and trip _build_task_nudge into returning a task nudge instead.
    # Pre-#1004 the test passed by accident: the resolver short-
    # circuited to a per-project tmp_path/.pollypm/state.db that the
    # supervisor's own ensure_layout had created empty. The post-#1004
    # resolver no longer routes per-project — pinning workspace_root
    # is the correct way to scope the nudge query.
    config.project.workspace_root = tmp_path
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="Ship the fix",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name="worker-pollypm",
        active=False,
        pane_id="%42",
        pane_current_command="codex",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )

    for index in range(4):
        supervisor.store.record_heartbeat(
            session_name="worker",
            tmux_window=window.name,
            pane_id=window.pane_id,
            pane_command=window.pane_current_command,
            pane_dead=False,
            log_bytes=100 + index,
            snapshot_path=str(tmp_path / f"snapshot-{index}.txt"),
            snapshot_hash="same-hash",
        )
    supervisor.store.record_heartbeat(
        session_name="worker",
        tmux_window=window.name,
        pane_id=window.pane_id,
        pane_command=window.pane_current_command,
        pane_dead=False,
        log_bytes=200,
        snapshot_path=str(tmp_path / "snapshot-current.txt"),
        snapshot_hash="same-hash",
    )

    sent: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        supervisor,
        "send_input",
        lambda session_name, text, owner="pollypm", force=False, press_enter=True: sent.append(
            (session_name, text, force)
        ),
    )
    # #765 classifier gates suspected_loop on has_pending_work — simulate
    # a queued task so this stall-nudge test keeps its original scope.
    monkeypatch.setattr(
        "pollypm.heartbeats.stall_classifier.has_pending_work_for_session",
        lambda config, session_name: True,
    )

    alerts = supervisor._update_alerts(
        launch,
        window,
        pane_text="Still stalled",
        previous_log_bytes=150,
        previous_snapshot_hash="same-hash",
        current_log_bytes=200,
        current_snapshot_hash="same-hash",
    )

    assert "suspected_loop" in alerts
    assert sent == [("worker", Supervisor._STALL_NUDGE_MESSAGE, False)]


def test_stalled_worker_nudge_skips_when_human_holds_lease(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="pollypm",
        prompt="Ship the fix",
        window_name="worker-pollypm",
    )
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name="worker-pollypm",
        active=False,
        pane_id="%42",
        pane_current_command="codex",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )

    for index in range(4):
        supervisor.store.record_heartbeat(
            session_name="worker",
            tmux_window=window.name,
            pane_id=window.pane_id,
            pane_command=window.pane_current_command,
            pane_dead=False,
            log_bytes=100 + index,
            snapshot_path=str(tmp_path / f"snapshot-{index}.txt"),
            snapshot_hash="same-hash",
        )
    supervisor.store.record_heartbeat(
        session_name="worker",
        tmux_window=window.name,
        pane_id=window.pane_id,
        pane_command=window.pane_current_command,
        pane_dead=False,
        log_bytes=200,
        snapshot_path=str(tmp_path / "snapshot-current.txt"),
        snapshot_hash="same-hash",
    )
    supervisor.claim_lease("worker", "human", "manual takeover")

    monkeypatch.setattr(
        supervisor,
        "send_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nudge should be skipped")),
    )
    # #765 classifier gates suspected_loop on has_pending_work — simulate
    # a queued task so the rest of the lease-skip assertion still runs.
    monkeypatch.setattr(
        "pollypm.heartbeats.stall_classifier.has_pending_work_for_session",
        lambda config, session_name: True,
    )

    alerts = supervisor._update_alerts(
        launch,
        window,
        pane_text="Still stalled",
        previous_log_bytes=150,
        previous_snapshot_hash="same-hash",
        current_log_bytes=200,
        current_snapshot_hash="same-hash",
    )

    assert "suspected_loop" in alerts
    # #349: the nudge-skip audit event lands in ``messages`` via the
    # sync Store path so this assertion sees it immediately.
    events = supervisor.msg_store.query_messages(
        type="event", scope="worker", limit=5,
    )
    assert any(
        event["scope"] == "worker"
        and event["subject"] == "heartbeat_nudge_skipped"
        and "leased to human" in (event.get("payload", {}).get("message") or "")
        for event in events
    )


def test_recovery_hard_limit_stops_after_many_failures(tmp_path: Path) -> None:
    from datetime import datetime, UTC
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    # Simulate many recovery attempts within the current window
    # so _record_recovery_attempt increments past the hard limit.
    now = datetime.now(UTC).isoformat()
    supervisor.store.upsert_session_runtime(
        session_name="operator",
        status="recovering",
        recovery_attempts=21,  # already past hard limit of 20
        recovery_window_started_at=now,  # current window so attempts accumulate
    )

    launch = next(item for item in supervisor.plan_launches() if item.session.name == "operator")
    supervisor._maybe_recover_session(
        launch,
        failure_type="missing_window",
        failure_message="window gone",
    )

    runtime = supervisor.store.get_session_runtime("operator")
    assert runtime.status == "degraded"
    alerts = supervisor.open_alerts()
    recovery_alerts = [a for a in alerts if a.alert_type == "recovery_limit"]
    assert len(recovery_alerts) == 1
    assert "STOPPED" in recovery_alerts[0].message
    assert "manual intervention" in recovery_alerts[0].message


# ---------------------------------------------------------------------------
# _build_review_nudge mtime cache (#174)
# ---------------------------------------------------------------------------


def _configure_review_nudge_projects(tmp_path: Path, n_projects: int) -> PollyPMConfig:
    """Build a config with *n_projects* projects, all sharing the collapsed
    workspace-scope DB (#339). Each project gets one in-review task seeded
    into that single DB. Used by the #174 cache tests — even though the
    DB is shared, ``_review_tasks_for_project`` still caches per-project by
    db_path mtime, and ``_build_review_nudge`` still iterates projects.
    """
    from pollypm.work.models import (
        Artifact as _Artifact,
        ArtifactKind as _ArtifactKind,
        OutputType as _OutputType,
        WorkOutput as _WorkOutput,
    )
    from pollypm.work.sqlite_service import SQLiteWorkService

    config = _config(tmp_path)
    # Pin workspace_root so _resolve_db_path lands inside tmp_path rather
    # than the real ~/dev — the test runs with a pytest-isolated HOME but
    # workspace_root defaults to Path.home() / "dev".
    config.project.workspace_root = tmp_path
    # Drop the boilerplate ``pollypm`` project from _config so the only
    # projects the nudge scans are the ones we seed here — the assertions
    # count opens per registered project.
    config.projects.clear()
    workspace_db = tmp_path / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)

    for i in range(n_projects):
        key = f"proj_{i}"
        proj_root = tmp_path / key
        (proj_root / ".pollypm").mkdir(parents=True, exist_ok=True)
        config.projects[key] = KnownProject(
            key=key, path=proj_root, name=key, kind=ProjectKind.FOLDER,
        )
        # All tasks land in the single workspace-scope DB; the ``project``
        # column is what isolates them (row-level scoping, post-#339).
        with SQLiteWorkService(db_path=workspace_db) as svc:
            task = svc.create(
                title=f"review task {i}",
                description="d",
                type="task",
                project=key,
                flow_template="standard",
                roles={"worker": "pete", "reviewer": "polly"},
                priority="normal",
                created_by="tester",
            )
            svc.queue(task.task_id, "pm")
            svc.claim(task.task_id, "pete")
            svc.node_done(
                task.task_id,
                "pete",
                _WorkOutput(
                    type=_OutputType.CODE_CHANGE,
                    summary="done",
                    artifacts=[
                        _Artifact(
                            kind=_ArtifactKind.COMMIT,
                            description="feat",
                            ref="abc",
                        ),
                    ],
                ),
            )
    return config


def _pin_load_config(monkeypatch, config: PollyPMConfig) -> None:
    """Route ``_resolve_db_path`` through *config* instead of the user's real
    on-disk config. ``work.cli._resolve_db_path`` imports ``load_config``
    lazily and calls it with no arg.
    """
    import pollypm.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_config", lambda path=None: config)


def test_build_review_nudge_caches_by_db_mtime(monkeypatch, tmp_path: Path) -> None:
    """Repeated nudge builds on unchanged dbs must not re-open SQLite (#174)."""
    from pollypm import supervisor as _supervisor_module

    config = _configure_review_nudge_projects(tmp_path, n_projects=4)
    _pin_load_config(monkeypatch, config)
    sup = Supervisor(config)

    # Reset cache to a clean slate before measuring.
    _supervisor_module._REVIEW_NUDGE_CACHE.clear()

    # Count SQLiteWorkService instantiations. Each instantiation is one
    # sqlite3.connect + schema check — the exact hotspot flagged by #174.
    opens: list[Path] = []
    from pollypm.work.sqlite_service import SQLiteWorkService as _RealSvc
    orig_init = _RealSvc.__init__

    def counting_init(self, *args, **kwargs):
        opens.append(kwargs.get("db_path", args[0] if args else None))
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(_RealSvc, "__init__", counting_init)

    # First call: cache is cold, must open one connection per project (4).
    nudge1 = sup._build_review_nudge()
    assert nudge1 is not None
    assert "4 tasks waiting" in nudge1
    first_opens = len(opens)
    assert first_opens == 4, f"cold pass opened {first_opens} connections (want 4)"

    # Second call: nothing changed — cache must serve every project with
    # zero new SQLite opens.
    opens.clear()
    nudge2 = sup._build_review_nudge()
    assert nudge2 == nudge1
    second_opens = len(opens)
    assert second_opens == 0, (
        f"warm pass opened {second_opens} connections (want 0) — "
        f"mtime cache is not short-circuiting"
    )

    # Mutate the workspace-scope db (touch mtime). All cached entries
    # share the same db_path post-#339, so every project cache row is
    # invalidated at once — the warm pass reopens once per project.
    touched_db = tmp_path / ".pollypm" / "state.db"
    import os as _os
    import time as _time
    new_mtime = touched_db.stat().st_mtime + 5.0
    _os.utime(touched_db, (new_mtime, new_mtime))
    # Give the filesystem a cycle to settle (macOS APFS can coalesce mtime).
    _time.sleep(0.01)

    opens.clear()
    sup._build_review_nudge()
    third_opens = len(opens)
    assert third_opens == 4, (
        f"db-changed pass opened {third_opens} connections "
        f"(want 4 — one reopen per project after mtime bump)"
    )


def test_build_review_nudge_evicts_dropped_projects(monkeypatch, tmp_path: Path) -> None:
    """Projects removed from config must be evicted from the cache (#174)."""
    from pollypm import supervisor as _supervisor_module

    config = _configure_review_nudge_projects(tmp_path, n_projects=3)
    _pin_load_config(monkeypatch, config)
    sup = Supervisor(config)

    _supervisor_module._REVIEW_NUDGE_CACHE.clear()
    sup._build_review_nudge()
    assert set(_supervisor_module._REVIEW_NUDGE_CACHE) >= {"proj_0", "proj_1", "proj_2"}

    # Drop proj_1 from config, rebuild — it must be evicted.
    del config.projects["proj_1"]
    sup._build_review_nudge()
    assert "proj_1" not in _supervisor_module._REVIEW_NUDGE_CACHE
    assert "proj_0" in _supervisor_module._REVIEW_NUDGE_CACHE
    assert "proj_2" in _supervisor_module._REVIEW_NUDGE_CACHE


# ---------------------------------------------------------------------------
# _send_initial_input_if_fresh — role gating (Issue #260)
# ---------------------------------------------------------------------------


def _make_launch_for_role(
    config: PollyPMConfig, tmp_path: Path, role: str, *, initial_input: str = "hello worker"
) -> SessionLaunchSpec:
    """Build a SessionLaunchSpec with a real fresh-launch marker on disk.

    Also registers the session in ``config.sessions`` so the persona-swap
    guard introduced in #266 (``_assert_session_launch_matches``) can
    resolve a matching launch via ``launch_by_session``. Without this,
    the guard raises ``persona_swap_detected: no launch for session_name=...``
    before ``send_keys`` is ever reached.
    """
    session = SessionConfig(
        name=f"{role}-session",
        role=role,
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path,
        project="pollypm",
        window_name=f"pm-{role}",
    )
    # Register the session so the planner can resolve it and the
    # persona-swap guard finds a matching launch (#266).
    config.sessions[session.name] = session
    marker_dir = tmp_path / ".pollypm" / "fresh-markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{role}-session.marker"
    marker.write_text("fresh\n")
    return SessionLaunchSpec(
        session=session,
        account=config.accounts["claude_controller"],
        window_name=f"pm-{role}",
        log_path=tmp_path / "logs" / f"{role}.log",
        command="claude",
        initial_input=initial_input,
        fresh_launch_marker=marker,
    )


def test_send_initial_input_delivers_prompt_for_worker_role(monkeypatch, tmp_path: Path) -> None:
    """Regression for Issue #260: workers on fresh launch must receive initial prompt."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(config, tmp_path, "worker", initial_input="worker kickoff")
    assert launch.fresh_launch_marker is not None
    assert launch.fresh_launch_marker.exists()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    # Skip the 0.5s sleep in the method.
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-worker")

    assert len(sent) == 1, "worker role should receive one send_keys call"
    assert sent[0][0] == "pollypm:pm-worker"
    assert sent[0][1] == "worker kickoff"
    assert not launch.fresh_launch_marker.exists(), "fresh-launch marker must be consumed"


def test_send_initial_input_still_delivers_for_reviewer_role(monkeypatch, tmp_path: Path) -> None:
    """Sanity: pre-existing control roles (reviewer) keep working — no regression."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(config, tmp_path, "reviewer", initial_input="reviewer kickoff")
    assert launch.fresh_launch_marker is not None

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-reviewer")

    assert len(sent) == 1
    assert sent[0][1] == "reviewer kickoff"
    assert not launch.fresh_launch_marker.exists()


def test_send_initial_input_skips_unknown_role(monkeypatch, tmp_path: Path) -> None:
    """Unknown / non-opted-in roles (e.g. cockpit) must NOT receive the prompt."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(config, tmp_path, "cockpit", initial_input="should not send")
    assert launch.fresh_launch_marker is not None
    marker_before = launch.fresh_launch_marker.exists()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-cockpit")

    assert sent == [], "non-opted-in role must not receive send_keys"
    # Marker is NOT consumed when role gate rejects.
    assert marker_before is True
    assert launch.fresh_launch_marker.exists()


# ---------------------------------------------------------------------------
# Pre-send pane guard — issue #931 (cockpit Polly · chat double-bootstrap)
# ---------------------------------------------------------------------------
#
# The user reported clicking "Polly · chat" in the rail and seeing the
# operator pane receive a heartbeat kickoff first ("Read /heartbeat.md…")
# followed by the operator kickoff — the agent correctly refused the
# identity swap and stalled. The pre-send guard reads the target pane
# before sending and refuses any kickoff whose role doesn't match the
# canonical role banner already present in the pane.


def test_send_initial_input_skips_when_pane_already_has_other_role_banner(
    monkeypatch, tmp_path: Path,
) -> None:
    """#931 — operator kickoff must not stack on top of a heartbeat banner.

    Simulates the bug: operator launch's target points at a pane whose
    capture already shows ``CANONICAL ROLE: heartbeat-supervisor``. The
    pre-send guard must skip the send (no double-bootstrap) and keep the
    fresh-launch marker on disk so the next attempt with the right
    target still works.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "operator-pm", initial_input="operator kickoff",
    )
    assert launch.fresh_launch_marker is not None

    # Pane shows a heartbeat banner — i.e. a heartbeat kickoff already
    # landed here. Operator kickoff must NOT stack.
    heartbeat_banner = (
        "======================================================================\n"
        "CANONICAL ROLE: heartbeat-supervisor\n"
        "SESSION NAME:   heartbeat\n"
        "======================================================================\n"
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: heartbeat_banner,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-operator")

    assert sent == [], (
        "operator kickoff must NOT be sent into a pane already bootstrapped "
        "as heartbeat-supervisor — that's the #931 double-bootstrap bug"
    )
    # Marker stays on disk so the next attempt with the correct target works.
    assert launch.fresh_launch_marker.exists()


def test_send_initial_input_proceeds_when_pane_has_matching_role_banner(
    monkeypatch, tmp_path: Path,
) -> None:
    """#931 — guard is no-op when banner matches role (idempotent re-send).

    A pane that already shows the operator banner should still accept an
    operator kickoff (the persona-verify backstop occasionally re-fires a
    legitimate kickoff and stacking the same role's banner is harmless;
    the guard only refuses *crossed* roles).
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "operator-pm", initial_input="operator kickoff",
    )
    matching_banner = (
        "======================================================================\n"
        "CANONICAL ROLE: operator-pm\n"
        "SESSION NAME:   operator-pm-session\n"
        "======================================================================\n"
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: matching_banner,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-operator")

    assert len(sent) == 1, (
        "operator kickoff with operator banner already in pane should still "
        "deliver — the guard only refuses crossed roles"
    )
    assert sent[0][1] == "operator kickoff"


def test_send_initial_input_proceeds_for_fresh_pane(
    monkeypatch, tmp_path: Path,
) -> None:
    """#931 — fresh pane (no banner) should bootstrap normally.

    The default case: the pane is empty (or shows the provider's intro)
    and there is no canonical role banner anywhere. The guard must not
    interfere with a clean kickoff.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "operator-pm", initial_input="operator kickoff",
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "Welcome to Claude Code\n>",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:pm-operator")

    assert len(sent) == 1
    assert sent[0][1] == "operator kickoff"


def test_send_initial_input_per_task_worker_pane_unaffected(
    monkeypatch, tmp_path: Path,
) -> None:
    """#931 — per-task worker (post-#919) kickoff path must not regress.

    Per-task workers are launched into uniquely named ``task-<project>-<N>``
    windows in the storage closet (#919/#921/#924). They share the pre-send
    guard via the session_services.tmux helper, but the worker role never
    crosses with operator/heartbeat windows in practice. Verify a fresh
    worker pane still gets its kickoff cleanly with no surprise rejections.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "worker", initial_input="worker kickoff",
    )
    # Pane is fresh — no banners yet (per-task panes wouldn't have the
    # operator-pm or heartbeat-supervisor banner crossed in).
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "pollypm:task-pollypm-1")

    assert len(sent) == 1, "per-task worker kickoff must still deliver"
    assert sent[0][0] == "pollypm:task-pollypm-1"
    assert sent[0][1] == "worker kickoff"
    assert not launch.fresh_launch_marker.exists()


def test_pane_already_bootstrapped_helper_recognises_other_role_banner(
    tmp_path: Path,
) -> None:
    """#931 — direct unit test for the pane-content classifier.

    The helper is the single seam where the (launch, target) crossed-tuple
    defense lives; both the supervisor kickoff path and the recovery-prompt
    path consult it. Pin its semantics so future refactors don't silently
    break the cross-role check.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    class _FakeTmux:
        def __init__(self, pane: str) -> None:
            self.pane = pane

        def capture_pane(self, target: str, lines: int = 0) -> str:
            return self.pane

    # Banner for a different role → guard fires.
    supervisor.session_service.tmux = _FakeTmux(
        "CANONICAL ROLE: heartbeat-supervisor\n"
    )
    assert supervisor._pane_already_bootstrapped_as_other_role(
        "operator-pm", "any-target",
    ) is True

    # Banner for the same role → guard is no-op.
    supervisor.session_service.tmux = _FakeTmux(
        "CANONICAL ROLE: operator-pm\n"
    )
    assert supervisor._pane_already_bootstrapped_as_other_role(
        "operator-pm", "any-target",
    ) is False

    # No banner at all → guard is no-op.
    supervisor.session_service.tmux = _FakeTmux("Welcome to Claude Code\n>")
    assert supervisor._pane_already_bootstrapped_as_other_role(
        "operator-pm", "any-target",
    ) is False

    # Empty role argument → guard short-circuits to False.
    assert supervisor._pane_already_bootstrapped_as_other_role(
        "", "any-target",
    ) is False


# ---------------------------------------------------------------------------
# Target-window pane guard — issue #932 (primary crossed-(launch, target))
# ---------------------------------------------------------------------------
#
# #931's banner-based guard catches the *secondary* kickoff send (after
# another role's banner has already landed in the pane). It cannot catch
# a crossed kickoff fired into a pristine pane that *belongs* to another
# session — the pane has no banner yet, so the banner classifier
# returns ``no banner present``. The user reported this as "Polly · chat
# loads, the agent reads heartbeat.md (the wrong primary), then
# operator.md is correctly blocked by #931's guard but the agent has
# already adopted the heartbeat identity".
#
# The fix: resolve the target pane to its tmux window and refuse the
# send unless ``pane.window_name == launch.window_name``. The dispatcher
# becomes explicit per pane: each (launch, target) tuple is verified at
# the boundary, regardless of upstream wiring.


def _make_pane(window_name: str, pane_id: str = "%fake") -> object:
    """Build a minimal pane stub that exposes window_name + pane_id."""
    return type(
        "Pane", (),
        {
            "window_name": window_name,
            "pane_id": pane_id,
            "pane_left": 0,
            "pane_current_command": "claude",
            "pane_dead": False,
        },
    )()


def test_send_initial_input_refuses_kickoff_when_target_in_other_window(
    monkeypatch, tmp_path: Path,
) -> None:
    """#932 — heartbeat kickoff into an operator pane is refused before send.

    Simulates the live failure: the operator pane (window pm-operator,
    fresh / no banner) is somehow handed to ``_send_initial_input_if_fresh``
    paired with the *heartbeat* launch. The new target-window guard must
    refuse the send because ``launch.window_name == "pm-heartbeat"`` does
    not match the target pane's actual window (``pm-operator``). The
    fresh-launch marker stays on disk so the next attempt with the right
    target still works.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "heartbeat-supervisor",
        initial_input="heartbeat kickoff",
    )
    # Crossed (launch, target): the target pane lives in pm-operator-pm
    # (the cockpit's right pane after _show_live_session joined it from
    # storage closet), but the launch is heartbeat. Guard must refuse.
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-operator-pm", pane_id="%op")],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "%op")

    assert sent == [], (
        "heartbeat kickoff must NOT land in a pane that belongs to "
        "pm-operator — that's the #932 crossed-primary bug"
    )
    # Marker stays on disk so the next attempt with the right target works.
    assert launch.fresh_launch_marker.exists()


def test_send_initial_input_proceeds_when_target_window_matches_launch(
    monkeypatch, tmp_path: Path,
) -> None:
    """#932 — kickoff proceeds when the target pane is in the launch's window.

    The mainline case: operator launch fires its kickoff into a pane
    that lives in window ``pm-operator``. The target-window guard must
    pass through and let the send happen. Pane is otherwise fresh (no
    banner) so the #931 banner guard is a no-op.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "operator-pm", initial_input="operator kickoff",
    )
    # Target pane lives in pm-operator-pm — matches launch.window_name.
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-operator-pm", pane_id="%op")],
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "Welcome to Claude Code\n>",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "%op")

    assert len(sent) == 1, "matching window should accept the kickoff"
    assert sent[0][1] == "operator kickoff"
    assert not launch.fresh_launch_marker.exists()


def test_send_initial_input_heartbeat_pane_skips_kickoff(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1007 — heartbeat-supervisor pane no longer receives a kickoff.

    Direction 2: the heartbeat tick loop runs as Python in
    :class:`pollypm.heartbeat.boot.HeartbeatRail` (a daemon thread in
    the cockpit/supervisor process). The ``pm-heartbeat`` Claude pane
    is observability-only — bootstrapping it tripped Claude's prompt-
    injection defense and the agent rejected the bootstrap as an
    injection attempt. Direction 2 stops sending the kickoff at all;
    the pane stays a dormant REPL the user can chat with ad-hoc.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "heartbeat-supervisor",
        initial_input="heartbeat kickoff",
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-heartbeat-supervisor", pane_id="%hb")],
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "%hb")

    # Direction 2: no kickoff sent, no fresh-launch marker consumed.
    assert sent == []


def test_send_initial_input_per_task_worker_window_matches(
    monkeypatch, tmp_path: Path,
) -> None:
    """#932 — per-task worker (post-#919) launch routes to its task-* window.

    Per-task workers spawn into ``task-<project>-<N>`` windows. Verify the
    target-window guard passes through when target is a pane in the
    matching ``task-<project>-<N>`` window — the per-task naming
    convention from #919 is preserved end-to-end.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    # Build a worker launch whose window_name matches the per-task naming.
    launch = _make_launch_for_role(
        config, tmp_path, "worker", initial_input="worker kickoff",
    )
    # Override window_name to the per-task convention. The launch builder
    # defaults to pm-worker; #919 puts per-task workers at task-<proj>-<N>.
    from dataclasses import replace as _replace
    launch = _replace(launch, window_name="task-pollypm-7")

    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [
            _make_pane(window_name="task-pollypm-7", pane_id="%task"),
        ],
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "%task")

    assert len(sent) == 1, "per-task worker kickoff must still deliver"
    assert sent[0][1] == "worker kickoff"


def test_target_window_helper_short_circuits_on_probe_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    """#932 — the helper is conservative: probe failure must NOT block sends.

    If ``list_panes`` raises (transient tmux error, race against window
    teardown, etc.) the guard must return True so the kickoff still
    proceeds. The role-banner guard (#931) and the persona-verify
    backstop continue to layer defensively. Suppressing legitimate
    kickoffs on a probe blip would regress the user-visible behavior
    far worse than the crossed-tuple it's defending against.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launch = _make_launch_for_role(
        config, tmp_path, "operator-pm", initial_input="operator kickoff",
    )

    def _raises(target: str) -> list[object]:
        raise RuntimeError("tmux: no current target")

    monkeypatch.setattr(
        supervisor.session_service.tmux, "list_panes", _raises,
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    supervisor._send_initial_input_if_fresh(launch, "%anything")

    assert len(sent) == 1, (
        "transient list_panes failure must not block the kickoff — "
        "the guard is fail-open by design"
    )


def test_target_window_helper_recognises_crossed_pane_via_send_log(
    monkeypatch, tmp_path: Path,
) -> None:
    """#932 — observable kickoff sequence, the unit-test analogue of the
    cockpit smoke test the user requested.

    Drives two consecutive kickoffs against the supervisor's send-keys
    log. Reviewer launch → pm-reviewer pane (must succeed), operator
    launch → pm-operator pane (must succeed). Then the *crossed*
    attempt — reviewer launch into the pm-operator pane — must produce
    no additional send. Replicates the ordering invariant from #932
    without any tmux interaction.

    #1007: previously this test used heartbeat-supervisor as the
    "legitimate first send" role, but Direction 2 (#1007) excludes
    heartbeat-supervisor from kickoff entirely (the heartbeat tick
    loop runs as Python in HeartbeatRail; the agent pane is
    observability-only). Reviewer is the simplest control role still
    in ``_INITIAL_INPUT_ROLES`` and exercises the same crossed-pane
    guard.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    reviewer_launch = _make_launch_for_role(
        config, tmp_path, "reviewer",
        initial_input="reviewer kickoff",
    )
    operator_launch = _make_launch_for_role(
        config, tmp_path, "operator-pm",
        initial_input="operator kickoff",
    )

    pane_window = {
        "%rv": "pm-reviewer",
        "%op": "pm-operator-pm",
    }
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [
            _make_pane(window_name=pane_window.get(target, ""), pane_id=target),
        ],
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=None: "",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )
    monkeypatch.setattr(supervisor, "_verify_input_submitted", lambda *a, **kw: None)
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda *_: None)

    # Step 1: legitimate reviewer kickoff to its own pane.
    supervisor._send_initial_input_if_fresh(reviewer_launch, "%rv")
    # Step 2: legitimate operator kickoff to its own pane.
    supervisor._send_initial_input_if_fresh(operator_launch, "%op")
    # Step 3: the bug — reviewer launch into the operator pane. Must
    # be refused without ever calling send_keys.
    # The fresh-launch marker for the reviewer was consumed in Step 1,
    # so use a fresh launch built with a brand-new marker.
    crossed_launch = _make_launch_for_role(
        config, tmp_path, "reviewer",
        initial_input="reviewer kickoff (crossed)",
    )
    supervisor._send_initial_input_if_fresh(crossed_launch, "%op")

    targets_sent = [t for t, _ in sent]
    texts_sent = [text for _, text in sent]
    assert targets_sent == ["%rv", "%op"], (
        f"only the two legitimate sends should land — got {targets_sent!r}"
    )
    assert "reviewer kickoff (crossed)" not in texts_sent, (
        "the crossed reviewer kickoff into the operator pane must NOT "
        "have been delivered"
    )


def test_start_cockpit_tui_respawns_rail_pane_with_restart_loop(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    panes = [
        type("Pane", (), {"pane_id": "%right", "pane_left": 42})(),
        type("Pane", (), {"pane_id": "%left", "pane_left": 0})(),
    ]
    respawns: list[tuple[str, str]] = []

    monkeypatch.setattr(supervisor.session_service.tmux, "list_panes", lambda target: panes)
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "respawn_pane",
        lambda target, command: respawns.append((target, command)),
    )

    supervisor.start_cockpit_tui("pollypm")

    assert len(respawns) == 1
    target, command = respawns[0]
    assert target == "%left"
    assert command.startswith("while true; do ")
    assert "pm cockpit" in command
    assert "[Rail exited" in command


def test_start_cockpit_tui_skips_respawn_when_rail_is_running(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    panes = [
        type(
            "Pane",
            (),
            {
                "pane_id": "%left",
                "pane_left": 0,
                "pane_current_command": "python",
                "pane_dead": False,
            },
        )(),
        type(
            "Pane",
            (),
            {
                "pane_id": "%right",
                "pane_left": 42,
                "pane_current_command": "python",
                "pane_dead": False,
            },
        )(),
    ]
    respawns: list[tuple[str, str]] = []

    monkeypatch.setattr(supervisor.session_service.tmux, "list_panes", lambda target: panes)
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "respawn_pane",
        lambda target, command: respawns.append((target, command)),
    )

    supervisor.start_cockpit_tui("pollypm")

    assert respawns == []


# ---------------------------------------------------------------------------
# #1007 — heartbeat-supervisor Direction 2: pane is observability-only
# ---------------------------------------------------------------------------


def test_initial_input_roles_excludes_heartbeat_supervisor() -> None:
    """#1007 — ``heartbeat-supervisor`` is not in the kickoff role set.

    Direction 2: the heartbeat tick loop runs as Python in
    :class:`pollypm.heartbeat.boot.HeartbeatRail` (a daemon thread in
    the cockpit/supervisor process). The agent pane is observability-
    only — bootstrapping it tripped Claude's prompt-injection defense
    and the agent rejected the bootstrap as an injection attempt.
    Direction 2 stops sending the kickoff at all.
    """
    assert "heartbeat-supervisor" not in Supervisor._INITIAL_INPUT_ROLES
    # Sanity: the other control roles still get bootstrapped.
    assert "operator-pm" in Supervisor._INITIAL_INPUT_ROLES
    assert "reviewer" in Supervisor._INITIAL_INPUT_ROLES
    assert "worker" in Supervisor._INITIAL_INPUT_ROLES
    assert "architect" in Supervisor._INITIAL_INPUT_ROLES


def test_restart_session_skips_recovery_prompt_for_heartbeat(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1007 — ``restart_session`` does not inject a recovery prompt
    into the heartbeat-supervisor pane.

    The previous behaviour stacked "RECOVERY MODE: RESUMING FROM
    CHECKPOINT … last state was heartbeat-supervisor" messages on
    every restart, which Claude's injection defense rejected as a
    pseudo-system-authority assertion. Direction 2 (#1007) drops the
    injection — the Python heartbeat loop kept ticking through the
    restart, so there is nothing to resume.

    The post-injection cleanup (runtime status update + alert
    clearing) MUST still run; only the send_keys path is skipped.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    # Stub the launch teardown / spawn path. We only care about the
    # send_keys log + the post-restart store mutations.
    monkeypatch.setattr(
        supervisor.session_service.tmux, "has_session", lambda _name: False,
    )
    monkeypatch.setattr(
        supervisor, "launch_session", lambda _name: None,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sent.append((target, text)),
    )

    supervisor.restart_session(
        "heartbeat", "claude_controller", failure_type="pane_dead",
    )

    # No recovery prompt sent into the pane.
    assert sent == [], (
        "heartbeat-supervisor recovery must not inject a recovery prompt"
    )
    # Post-recovery cleanup still runs.
    runtime = supervisor.store.get_session_runtime("heartbeat")
    assert runtime is not None
    assert runtime.status == "healthy"
