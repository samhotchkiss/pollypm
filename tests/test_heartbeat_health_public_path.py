from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from typer.testing import CliRunner

from pollypm.config import write_config
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


def _config(tmp_path: Path) -> PollyPMConfig:
    root = tmp_path / "workspace"
    base = root / ".pollypm"
    logs_dir = base / "logs"
    snapshots_dir = base / "snapshots"
    logs_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return PollyPMConfig(
        project=ProjectSettings(
            name="PublicHealthTest",
            root_dir=root,
            tmux_session="pollypm-test",
            workspace_root=tmp_path,
            base_dir=base,
            logs_dir=logs_dir,
            snapshots_dir=snapshots_dir,
            state_db=base / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=base / "homes" / "claude_controller",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=root,
                project="pollypm",
                window_name="pm-operator",
                auth_token="a" * 64,
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=root,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            ),
        },
    )


def _cli_app() -> typer.Typer:
    from pollypm.cli_features import sessions_health

    app = typer.Typer()
    sessions_health.register_sessions_health_command(app)

    @app.command("_marker")
    def _marker() -> None:  # pragma: no cover - keeps subcommand mode enabled
        return None

    return app


def test_heartbeat_rail_threads_active_config_path_into_core_recurring_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from pollypm.heartbeat import Roster
    from pollypm.heartbeat import boot as boot_mod
    from pollypm.jobs import JobHandlerRegistry

    class _FakeQueue:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.enqueued: list[dict[str, Any]] = []

        def enqueue(
            self,
            handler_name: str,
            payload: dict[str, Any],
            *,
            dedupe_key: str | None = None,
            run_after: datetime | None = None,
        ) -> int:
            self.enqueued.append(
                {
                    "handler_name": handler_name,
                    "payload": payload,
                    "dedupe_key": dedupe_key,
                    "run_after": run_after,
                }
            )
            return len(self.enqueued)

        def has_recent_or_active_dedupe(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

    class _FakePool:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.is_running = False

        def start(self, *, concurrency: int = 1) -> None:
            self.is_running = True

        def stop(self, *, timeout: float = 10.0) -> None:
            self.is_running = False

    registry = JobHandlerRegistry()
    registry.register(
        name="session.health_sweep",
        handler=lambda payload: {"payload": payload},
        plugin_name="core_recurring",
    )
    roster = Roster()
    roster.register(
        schedule="@on_startup",
        handler_name="session.health_sweep",
        payload={},
        dedupe_key="session.health_sweep",
    )

    class _FakeHost:
        def build_roster(self):
            return roster

        def job_handler_registry(self):
            return registry

    monkeypatch.setattr(boot_mod, "JobQueue", _FakeQueue)
    monkeypatch.setattr(boot_mod, "JobWorkerPool", _FakePool)
    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", lambda _config=None: object()
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_migrations.apply_migrations", lambda _pool: None
    )

    config_path = tmp_path / "alternate" / "pollypm.toml"
    rail = boot_mod.HeartbeatRail.from_plugin_host(
        state_db=tmp_path / "state.db",
        plugin_host=_FakeHost(),
        config_path=config_path,
        config=object(),
    )

    result = rail.tick(datetime.now(UTC))

    assert result.enqueued_count == 1
    assert result.enqueued[0].payload["config_path"] == str(config_path)


def test_session_health_sweep_write_is_visible_to_pm_sessions_health(
    pg_schema_pool,
    monkeypatch,
    tmp_path: Path,
) -> None:
    from pollypm.cli_features import sessions_health
    from pollypm.heartbeats.api import SupervisorHeartbeatAPI
    from pollypm.heartbeats.local import LocalHeartbeatBackend
    from pollypm.plugins_builtin.core_recurring import plugin as recurring_plugin
    from pollypm.storage.pg_migrations import apply_migrations
    import pollypm.supervisor as supervisor_mod

    apply_migrations(pg_schema_pool)

    config = _config(tmp_path)
    config_path = config.project.base_dir / "pollypm.toml"
    write_config(config, config_path, force=True)

    launch = SessionLaunchSpec(
        session=config.sessions["operator"],
        account=config.accounts["claude_controller"],
        window_name="pm-operator",
        log_path=config.project.logs_dir / "operator.log",
        command="claude",
    )
    launch.log_path.write_text("ready\n", encoding="utf-8")
    snapshot_path = config.project.snapshots_dir / "operator.txt"

    fake_window = SimpleNamespace(
        name="pm-operator",
        pane_id="%42",
        pane_current_command="claude",
        pane_dead=False,
        pane_pid=None,
    )

    def _plan_launches(self):
        return [launch]

    def _window_map(self):
        return {(self.storage_closet_session_name(), "pm-operator"): fake_window}

    def _write_snapshot(self, _window, _snapshot_lines):
        snapshot_path.write_text("operator is ready\n", encoding="utf-8")
        return snapshot_path, "operator is ready"

    monkeypatch.setattr(
        supervisor_mod, "sync_token_ledger_for_config", lambda _config: []
    )
    monkeypatch.setattr(
        supervisor_mod, "_load_sweep_stale_notifies_with_retry",
        lambda: lambda _store: None,
    )
    monkeypatch.setattr(supervisor_mod.Supervisor, "_check_fd_pressure", lambda self: None)
    monkeypatch.setattr(supervisor_mod.Supervisor, "plan_launches", _plan_launches)
    monkeypatch.setattr(supervisor_mod.Supervisor, "window_map", _window_map)
    monkeypatch.setattr(supervisor_mod.Supervisor, "write_snapshot", _write_snapshot)
    monkeypatch.setattr(
        supervisor_mod.Supervisor,
        "tmux_session_for_launch",
        lambda self, _launch: self.storage_closet_session_name(),
    )
    monkeypatch.setattr(
        supervisor_mod.Supervisor,
        "_sweep_stale_alerts",
        lambda self, **_kwargs: None,
    )
    monkeypatch.setattr(
        supervisor_mod.Supervisor,
        "_sweep_recovered_recovery_alerts",
        lambda self, **_kwargs: None,
    )
    monkeypatch.setattr(
        supervisor_mod.Supervisor, "ensure_heartbeat_schedule", lambda self: None
    )
    monkeypatch.setattr(
        SupervisorHeartbeatAPI,
        "record_checkpoint",
        lambda self, context, *, alerts: None,
    )
    monkeypatch.setattr(
        LocalHeartbeatBackend,
        "_dispatch_health_intervention",
        lambda self, api, context: None,
    )
    monkeypatch.setattr(
        recurring_plugin,
        "sweep_ephemeral_sessions",
        lambda *_args, **_kwargs: {
            "considered": 0,
            "alerts_raised": 0,
            "skipped_planned": 0,
            "zombie_task_windows_killed": 0,
        },
    )

    result = recurring_plugin.session_health_sweep_handler(
        {"config_path": str(config_path), "snapshot_lines": 20}
    )

    assert result["alerts_raised"] == 0

    monkeypatch.setattr(
        sessions_health,
        "_list_windows",
        lambda _name: {
            "pm-operator": SimpleNamespace(
                pane_current_command="claude", pane_pid=4242
            )
        },
    )
    cli_result = CliRunner().invoke(
        _cli_app(), ["sessions", "--json", "--config", str(config_path)]
    )

    assert cli_result.exit_code == 0, cli_result.output
    rows = [
        json.loads(line)
        for line in cli_result.output.splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "operator"
    assert row["status"] == "healthy"
    assert row["last_heartbeat_iso"]
    assert row["last_heartbeat_age"] != "none"
