"""Smoke tests for auth-related configuration and launch setup.

These tests verify the runtime paths that caused auth regressions:
- CLAUDE_CONFIG_DIR resolution
- .claude.json placement inside config dir
- Session markers cleared on bootstrap
- Launch commands wrapped in login shell
"""

import base64
import json
import shlex
from pathlib import Path

from pollypm.config import load_config, write_config, GLOBAL_CONFIG_DIR
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.onboarding import _prime_claude_home
from pollypm.runtimes.local import LocalRuntimeAdapter
from pollypm.runtime_env import claude_config_dir
from pollypm.supervisor import Supervisor


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="Test",
            root_dir=tmp_path,
            tmux_session="test-pm",
            base_dir=tmp_path / ".pollypm",
        ),
        pollypm=PollyPMSettings(controller_account="claude_test", failover_accounts=[]),
        accounts={
            "claude_test": AccountConfig(
                name="claude_test",
                provider=ProviderKind.CLAUDE,
                email="test@example.com",
                home=tmp_path / ".pollypm" / "homes" / "claude_test",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_test",
                cwd=tmp_path,
                window_name="pm-heartbeat",
            ),
        },
        projects={},
    )


def test_claude_config_dir_resolves_inside_account_home(tmp_path: Path) -> None:
    home = tmp_path / "homes" / "claude_test"
    home.mkdir(parents=True)
    config_dir = claude_config_dir(home)
    assert config_dir == home / ".claude"
    assert str(config_dir).endswith("/.claude")


def test_prime_claude_home_writes_state_inside_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "homes" / "claude_test"
    _prime_claude_home(home)
    state_inside = home / ".claude" / ".claude.json"
    assert state_inside.exists(), ".claude.json must be inside CLAUDE_CONFIG_DIR"
    data = json.loads(state_inside.read_text())
    assert data["hasCompletedOnboarding"] is True


def test_launch_command_wraps_in_login_shell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.project.state_db = tmp_path / ".pollypm" / "state.db"
    config.accounts["claude_test"].home.mkdir(parents=True)
    _prime_claude_home(config.accounts["claude_test"].home)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launches = supervisor.plan_launches()
    for launch in launches:
        assert launch.command.startswith("sh -lc "), (
            f"Launch command for {launch.session.name} must wrap in sh -lc"
        )


def test_bootstrap_clears_session_markers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    home = config.accounts["claude_test"].home
    home.mkdir(parents=True)
    _prime_claude_home(home)

    # Create fake session markers
    markers_dir = home / ".pollypm" / "session-markers"
    markers_dir.mkdir(parents=True)
    (markers_dir / "heartbeat.resume").write_text("stale")
    (markers_dir / "heartbeat.fresh").write_text("stale")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    # Simulate what _bootstrap_launches does
    supervisor._bootstrap_clear_markers()

    assert not (markers_dir / "heartbeat.resume").exists()
    assert not (markers_dir / "heartbeat.fresh").exists()


def test_claude_control_sessions_keep_original_account_home(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.project.state_db = tmp_path / ".pollypm" / "state.db"
    home = config.accounts["claude_test"].home
    home.mkdir(parents=True)
    _prime_claude_home(home)

    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launches = supervisor.plan_launches()
    hb = next(l for l in launches if l.session.name == "heartbeat")

    # Claude sessions must use the original home, not a control-homes copy
    assert hb.account.home == home
