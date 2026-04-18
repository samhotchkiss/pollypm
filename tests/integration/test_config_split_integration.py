import base64
import json
import shlex
from pathlib import Path

from typer.testing import CliRunner

import pollypm.cli as cli
from pollypm.service_api import PollyPMService


def _decode_launch_payload(command: str) -> dict[str, object]:
    parts = shlex.split(command)
    if parts[0] == "sh" and "-lc" in parts:
        parts = shlex.split(parts[-1])
    payload = parts[-1]
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return json.loads(raw.decode("utf-8"))


def _write_split_config(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "sample-project"
    project_root.mkdir()
    project_config_dir = project_root / ".pollypm" / "config"
    project_config_dir.mkdir(parents=True)
    (project_config_dir / "project.toml").write_text(
        """
[project]
display_name = "Sample Project"

[sessions.worker_sample]
role = "worker"
provider = "claude"
account = "claude_worker"
cwd = "."
window_name = "worker-sample"
prompt = "Implement issue #1."
"""
    )

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"""
[project]
name = "PollyPM"
tmux_session = "pollypm"
workspace_root = "{tmp_path / 'workspace'}"
base_dir = "{tmp_path / '.pollypm-state'}"
logs_dir = "{tmp_path / '.pollypm-state' / 'logs'}"
snapshots_dir = "{tmp_path / '.pollypm-state' / 'snapshots'}"
state_db = "{tmp_path / '.pollypm-state' / 'state.db'}"

[pollypm]
controller_account = "claude_controller"
failover_enabled = false

[accounts.claude_controller]
provider = "claude"
email = "controller@example.com"
home = "{tmp_path / '.pollypm-state' / 'homes' / 'claude_controller'}"

[accounts.claude_worker]
provider = "claude"
email = "worker@example.com"
home = "{tmp_path / '.pollypm-state' / 'homes' / 'claude_worker'}"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_controller"
cwd = "{tmp_path}"
window_name = "pm-heartbeat"

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_controller"
cwd = "{tmp_path}"
window_name = "pm-operator"

[projects.sample]
path = "{project_root}"
name = "Sample"
"""
    )
    return config_path, project_root


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.created_sessions: list[tuple[str, str, str]] = []
        self.created_windows: list[tuple[str, str, str, bool]] = []
        self.attached: list[str] = []

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def current_session_name(self):
        return None

    def create_session(self, name: str, window_name: str, command: str, **_kwargs) -> None:
        self.sessions.add(name)
        self.created_sessions.append((name, window_name, command))

    def create_window(self, name: str, window_name: str, command: str, detached: bool = False) -> None:
        self.created_windows.append((name, window_name, command, detached))

    def set_window_option(self, target: str, option: str, value: str) -> None:
        return None

    def pipe_pane(self, target: str, path: Path) -> None:
        return None

    def attach_session(self, name: str) -> int:
        self.attached.append(name)
        return 0

    def select_window(self, target: str) -> None:
        return None

    def kill_session(self, name: str) -> None:
        self.sessions.discard(name)


def test_split_config_plan_launches_discovers_project_local_worker(tmp_path: Path) -> None:
    config_path, project_root = _write_split_config(tmp_path)

    supervisor = PollyPMService(config_path).load_supervisor()
    launches = supervisor.plan_launches()

    assert [launch.session.name for launch in launches] == ["heartbeat", "operator", "worker_sample"]
    worker_launch = launches[-1]
    assert worker_launch.session.project == "sample"
    assert worker_launch.session.cwd == project_root
    assert worker_launch.window_name == "worker-sample"

    payload = _decode_launch_payload(worker_launch.command)
    assert payload["cwd"] == str(project_root)
    assert payload["argv"][0] == "claude"


def test_pm_up_bootstraps_worker_from_project_local_config(monkeypatch, tmp_path: Path) -> None:
    config_path, project_root = _write_split_config(tmp_path)
    fake_tmux = FakeTmux()

    monkeypatch.setattr("pollypm.session_services.tmux.TmuxClient", lambda: fake_tmux)
    monkeypatch.setattr("pollypm.supervisor.Supervisor._stabilize_launch", lambda self, launch, target, on_status=None: None)
    # ``_bootstrap_launches`` calls ``_stabilize_claude_launch`` directly from
    # worker threads (not through ``_stabilize_launch``). Without stubbing it
    # the per-thread poll loop runs to its 90s deadline against FakeTmux
    # (which has no ``capture_pane``), dominating the test wall time.
    monkeypatch.setattr(
        "pollypm.supervisor.Supervisor._stabilize_claude_launch",
        lambda self, target, on_status=None: None,
    )
    monkeypatch.setattr(
        "pollypm.supervisor.Supervisor._stabilize_codex_launch",
        lambda self, target, on_status=None, account=None: None,
    )
    # ``_send_initial_input_if_fresh`` does a 0.5s sleep before send_keys;
    # skip it — the test doesn't assert on initial input delivery.
    monkeypatch.setattr(
        "pollypm.supervisor.Supervisor._send_initial_input_if_fresh",
        lambda self, launch, target: None,
    )
    # ``cli.up`` sleeps 0.3s after the cockpit layout split. That sleep
    # lives in an inline ``import time`` so monkey-patching ``time.sleep``
    # on the module object is the simplest way to reach it. Scope is
    # auto-restored by ``monkeypatch`` at teardown.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda _s: None)
    monkeypatch.setattr("pollypm.supervisor.Supervisor.focus_console", lambda self: None)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Created tmux session pollypm with controller controller@example.com [claude]" in result.output

    created_window_names = [window_name for _session, window_name, _command, _detached in fake_tmux.created_windows]
    assert "worker-sample" in created_window_names

    worker_command = next(command for _session, window_name, command, _detached in fake_tmux.created_windows if window_name == "worker-sample")
    payload = _decode_launch_payload(worker_command)
    assert payload["cwd"] == str(project_root)
