from pathlib import Path

from promptmaster.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PromptMasterConfig,
    PromptMasterSettings,
    ProviderKind,
    SessionConfig,
)
from promptmaster.supervisor import (
    Supervisor,
    _extract_claude_model_name,
    _extract_codex_model_name,
    _extract_token_metrics,
)


def _config(tmp_path: Path) -> PromptMasterConfig:
    return PromptMasterConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".promptmaster",
            logs_dir=tmp_path / ".promptmaster/logs",
            snapshots_dir=tmp_path / ".promptmaster/snapshots",
            state_db=tmp_path / ".promptmaster/state.db",
        ),
        promptmaster=PromptMasterSettings(
            controller_account="claude_controller",
            failover_enabled=True,
            failover_accounts=["codex_backup"],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".promptmaster/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".promptmaster/homes/codex_backup",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="promptmaster",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="promptmaster",
                window_name="pm-operator",
            )
        },
        projects={
            "promptmaster": KnownProject(
                key="promptmaster",
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


def test_heartbeat_uses_separate_tmux_session(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    created_sessions: list[tuple[str, str, str]] = []
    created_windows: list[tuple[str, str, str]] = []
    piped_targets: list[str] = []

    monkeypatch.setattr(supervisor, "_probe_controller_account", lambda account_name: None)
    monkeypatch.setattr(supervisor, "_stabilize_launch", lambda launch, target: None)
    monkeypatch.setattr(supervisor, "_record_launch", lambda launch: None)
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda name: False)
    monkeypatch.setattr(
        supervisor.tmux,
        "create_session",
        lambda name, window_name, command: created_sessions.append((name, window_name, command)),
    )
    monkeypatch.setattr(
        supervisor.tmux,
        "create_window",
        lambda name, window_name, command, detached=False: created_windows.append((name, window_name, command, detached)),
    )
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: piped_targets.append(target))
    monkeypatch.setattr(supervisor, "ensure_console_window", lambda: None)
    monkeypatch.setattr(supervisor, "focus_console", lambda: None)

    controller = supervisor.bootstrap_tmux()

    assert controller == "claude_controller"
    assert created_sessions[0][0] == config.project.tmux_session
    assert created_sessions[0][1] == "pm-operator"
    assert created_sessions[1][0] == supervisor.heartbeat_tmux_session_name()
    assert created_sessions[1][1] == "pm-heartbeat"
    assert piped_targets == [f"{config.project.tmux_session}:0", f"{supervisor.heartbeat_tmux_session_name()}:0"]


def test_launch_session_creates_worker_window_detached(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="promptmaster",
        prompt="Inspect the repo",
        window_name="worker-promptmaster",
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
    monkeypatch.setattr(supervisor.tmux, "pipe_pane", lambda target, path: None)
    monkeypatch.setattr(supervisor, "_record_launch", lambda launch: None)
    monkeypatch.setattr(supervisor, "_stabilize_launch", lambda launch, target: None)

    supervisor.launch_session("worker")

    assert len(created_windows) == 1
    assert created_windows[0][0] == config.project.tmux_session
    assert created_windows[0][1] == "worker-promptmaster"
    assert created_windows[0][3] is True


def test_control_session_args_follow_override_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.sessions["heartbeat"].args = ["--dangerously-skip-permissions"]
    config.sessions["operator"].args = ["--dangerously-skip-permissions"]
    supervisor = Supervisor(config)

    launches = supervisor.plan_launches(controller_account="codex_backup")

    for launch in launches:
        assert launch.session.provider is ProviderKind.CODEX
        assert launch.session.args == ["--dangerously-bypass-approvals-and-sandbox"]


def test_control_sessions_use_dedicated_control_homes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source_home = config.accounts["claude_controller"].home
    assert source_home is not None
    (source_home / ".claude").mkdir(parents=True, exist_ok=True)
    (source_home / ".claude" / ".credentials.json").write_text('{"token":"base"}\n')
    (source_home / ".claude" / "settings.json").write_text('{"theme":"dark"}\n')
    (source_home / ".claude.json").write_text('{"hasCompletedOnboarding": true}\n')

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    heartbeat_home = launches["heartbeat"].account.home
    operator_home = launches["operator"].account.home

    assert heartbeat_home == tmp_path / ".promptmaster" / "control-homes" / "heartbeat"
    assert operator_home == tmp_path / ".promptmaster" / "control-homes" / "operator"
    assert heartbeat_home != operator_home
    assert (heartbeat_home / ".claude" / ".credentials.json").exists()
    assert (operator_home / ".claude" / ".credentials.json").exists()
    assert launches["heartbeat"].resume_marker == heartbeat_home / ".promptmaster" / "session-markers" / "heartbeat.resume"
    assert launches["operator"].resume_marker == operator_home / ".promptmaster" / "session-markers" / "operator.resume"


def test_control_sessions_use_agent_profiles_for_prompts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert "Polly" in launches["operator"].session.prompt
    assert "heartbeat supervisor" in launches["heartbeat"].session.prompt


def test_open_permissions_default_can_disable_launch_args(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.promptmaster.open_permissions_by_default = False
    config.sessions["worker"] = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_backup",
        cwd=tmp_path,
        project="promptmaster",
        prompt="Inspect the repo",
        window_name="worker-promptmaster",
    )
    supervisor = Supervisor(config)

    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}

    assert launches["heartbeat"].session.args == []
    assert launches["operator"].session.args == []
    assert launches["worker"].session.args == []


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
        "promptmaster.supervisor.get_heartbeat_backend",
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

    supervisor._stabilize_codex_launch("promptmaster:0")


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


def test_recovery_waits_on_non_promptmaster_lease(tmp_path: Path) -> None:
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
