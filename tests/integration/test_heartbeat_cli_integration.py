import json
from pathlib import Path

from typer.testing import CliRunner

import pollypm.cli as cli
from pollypm.storage.state import StateStore


def _write_cli_config(tmp_path: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    project_root = workspace_root / "demo"
    project_root.mkdir()
    config_path = tmp_path / "pollypm.toml"
    (project_root / ".pollypm" / "config").mkdir(parents=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        f"""
[project]
display_name = "Demo"
persona_name = "Dora"

[sessions.worker_demo]
role = "worker"
provider = "claude"
account = "claude_main"
cwd = "."
window_name = "worker-demo"
agent_profile = "worker"
"""
    )
    config_path.write_text(
        f"""
[project]
name = "PollyPM"
tmux_session = "pollypm"
workspace_root = "{workspace_root}"
base_dir = "{workspace_root / '.pollypm'}"
logs_dir = "{workspace_root / '.pollypm' / 'logs'}"
snapshots_dir = "{workspace_root / '.pollypm' / 'snapshots'}"
state_db = "{workspace_root / '.pollypm' / 'state.db'}"

[pollypm]
controller_account = "claude_main"

[accounts.claude_main]
provider = "claude"
home = "{workspace_root / '.pollypm' / 'homes' / 'claude_main'}"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_main"
cwd = "{workspace_root}"
window_name = "pm-heartbeat"

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_main"
cwd = "{workspace_root}"
window_name = "pm-operator"

[projects.demo]
path = "{project_root}"
name = "Demo"
persona_name = "Dora"
"""
    )
    return config_path


class FakeTmux:
    sent_keys: list[tuple[str, str, bool]] = []

    def has_session(self, name: str) -> bool:
        return False

    def list_windows(self, session_name: str):
        return []

    def send_keys(self, target: str, text: str, *, press_enter: bool = True) -> None:
        self.sent_keys.append((target, text, press_enter))


def test_heartbeat_cli_commands_round_trip_state(monkeypatch, tmp_path: Path) -> None:
    config_path = _write_cli_config(tmp_path)
    monkeypatch.setattr("pollypm.session_services.tmux.TmuxClient", lambda: FakeTmux())
    runner = CliRunner()

    result = runner.invoke(cli.app, ["status", "--config", str(config_path), "--json"])
    assert result.exit_code == 0
    status_payload = json.loads(result.output)
    assert [item["name"] for item in status_payload["sessions"]] == ["heartbeat", "operator", "worker_demo"]

    result = runner.invoke(
        cli.app,
        [
            "alert",
            "raise",
            "session_stuck",
            "worker_demo",
            "No progress",
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert result.exit_code == 0
    alert_payload = json.loads(result.output)["alert"]
    alert_id = alert_payload["alert_id"]
    assert alert_payload["session_name"] == "worker_demo"

    result = runner.invoke(cli.app, ["alert", "list", "--config", str(config_path), "--json"])
    assert result.exit_code == 0
    listed_alerts = json.loads(result.output)["alerts"]
    assert [item["alert_id"] for item in listed_alerts] == [alert_id]

    result = runner.invoke(
        cli.app,
        [
            "session",
            "set-status",
            "worker_demo",
            "needs_followup",
            "--reason",
            "Waiting on review",
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert result.exit_code == 0
    runtime_payload = json.loads(result.output)["session_runtime"]
    assert runtime_payload["status"] == "needs_followup"
    assert runtime_payload["last_failure_message"] == "Waiting on review"

    result = runner.invoke(
        cli.app,
        [
            "heartbeat",
            "record",
            "worker_demo",
            '{"snapshot_hash":"abc123","log_bytes":12,"snapshot_path":"snap.txt"}',
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert result.exit_code == 0
    heartbeat_payload = json.loads(result.output)["heartbeat"]
    assert heartbeat_payload["session_name"] == "worker_demo"
    assert heartbeat_payload["snapshot_hash"] == "abc123"

    result = runner.invoke(
        cli.app,
        ["alert", "clear", str(alert_id), "--config", str(config_path), "--json"],
    )
    assert result.exit_code == 0
    cleared = json.loads(result.output)["alert"]
    assert cleared["status"] == "cleared"

    store = StateStore((tmp_path / "workspace" / ".pollypm" / "state.db"))
    assert store.latest_heartbeat("worker_demo").snapshot_hash == "abc123"
    assert store.get_session_runtime("worker_demo").status == "needs_followup"
    assert store.open_alerts() == []
    store.close()


def test_send_command_respects_human_lease(monkeypatch, tmp_path: Path) -> None:
    config_path = _write_cli_config(tmp_path)
    fake_tmux = FakeTmux()
    monkeypatch.setattr("pollypm.session_services.tmux.TmuxClient", lambda: fake_tmux)
    store = StateStore((tmp_path / "workspace" / ".pollypm" / "state.db"))
    store.set_lease("worker_demo", "human", "busy")
    store.close()

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["send", "worker_demo", "Ping", "--owner", "heartbeat", "--config", str(config_path)],
    )

    assert result.exit_code != 0
    # The error message was updated to direct operators to use the inbox
    assert "Blocked" in result.output or "session is currently leased" in result.output
    assert fake_tmux.sent_keys == []
