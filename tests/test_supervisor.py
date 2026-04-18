import base64
import json
import shlex
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
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
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
                home=tmp_path / ".pollypm-state/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm-state/homes/codex_backup",
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
    events = supervisor.store.recent_events(limit=5)
    assert any(
        event.session_name == "operator"
        and event.event_type == "lease"
        and event.message == "Lease released"
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
    events = supervisor.store.recent_events(limit=5)
    assert any(
        event.session_name == "operator"
        and event.event_type == "lease"
        and "Auto-released expired lease held by human" in event.message
        for event in events
    )


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

    launches = supervisor.plan_launches(controller_account="codex_backup")

    for launch in launches:
        assert launch.session.provider is ProviderKind.CODEX
        assert launch.session.args == ["--sandbox", "read-only", "--ask-for-approval", "never"]


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


def test_codex_control_home_syncs_global_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.accounts["codex_backup"] = AccountConfig(
        name="codex_backup",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        home=tmp_path / ".pollypm-state" / "homes" / "codex_backup",
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
    assert launches["worker"].session.args == ["--sandbox", "workspace-write", "--ask-for-approval", "never"]


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

    assert argv == ["codex", "--sandbox", "read-only", "--ask-for-approval", "never"]
    assert "PM_CODEX_HOME_AGENTS_MD" in env
    assert "Polly" in env["PM_CODEX_HOME_AGENTS_MD"]
    assert launch.initial_input is None


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
    assert not any("do some work" in arg for arg in decoded["argv"])
    assert decoded["argv"] == ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "never"]
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
    events = supervisor.store.recent_events(limit=5)
    assert any(
        event.session_name == "worker"
        and event.event_type == "heartbeat_nudge_skipped"
        and "leased to human" in event.message
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
    """Build a config with *n_projects* project-scoped SQLite work dbs,
    each seeded with one in-review task. Used by the #174 cache tests.
    """
    from pollypm.work.models import (
        Artifact as _Artifact,
        ArtifactKind as _ArtifactKind,
        OutputType as _OutputType,
        WorkOutput as _WorkOutput,
    )
    from pollypm.work.sqlite_service import SQLiteWorkService

    config = _config(tmp_path)
    for i in range(n_projects):
        key = f"proj_{i}"
        proj_root = tmp_path / key
        (proj_root / ".pollypm").mkdir(parents=True, exist_ok=True)
        config.projects[key] = KnownProject(
            key=key, path=proj_root, name=key, kind=ProjectKind.FOLDER,
        )
        db_path = proj_root / ".pollypm" / "state.db"
        with SQLiteWorkService(db_path=db_path) as svc:
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
    assert "4 task(s) waiting" in nudge1
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

    # Mutate one project's db (touch mtime). Only that project should reopen.
    touched_db = config.projects["proj_1"].path / ".pollypm" / "state.db"
    import os as _os
    import time as _time
    new_mtime = touched_db.stat().st_mtime + 5.0
    _os.utime(touched_db, (new_mtime, new_mtime))
    # Give the filesystem a cycle to settle (macOS APFS can coalesce mtime).
    _time.sleep(0.01)

    opens.clear()
    sup._build_review_nudge()
    third_opens = len(opens)
    assert third_opens == 1, (
        f"one-project-changed pass opened {third_opens} connections (want 1)"
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
    marker_dir = tmp_path / ".pollypm-state" / "fresh-markers"
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
