"""Focused tests for :class:`pollypm.cockpit_project_settings.PollyProjectSettingsApp`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from textual.widgets import Button

from pollypm.model_registry import AliasRecord, Registry, RoleRequirements
from pollypm.models import ModelAssignment, ProviderKind


def _fake_role_registry() -> Registry:
    return Registry(
        aliases={
            "codex-gpt-5.4": AliasRecord(
                provider="codex",
                model="gpt-5.4",
                capabilities=("reasoning", "tool_use", "long_context", "strong_planning"),
            ),
            "opus-4.7": AliasRecord(
                provider="claude",
                model="claude-opus-4-7",
                capabilities=("reasoning", "tool_use", "long_context", "strong_planning"),
            ),
            "sonnet-4.6": AliasRecord(
                provider="claude",
                model="claude-sonnet-4-6",
                capabilities=("reasoning", "tool_use", "long_context"),
            ),
            "haiku-4.5": AliasRecord(
                provider="claude",
                model="claude-haiku-4-5-20251001",
                capabilities=("tool_use", "weak_planning"),
            ),
        },
        role_requirements={
            "architect": RoleRequirements(
                preferred=("strong_planning", "reasoning"),
                discouraged=("weak_planning",),
            ),
            "worker": RoleRequirements(
                preferred=("tool_use", "reasoning"),
                discouraged=(),
            ),
            "reviewer": RoleRequirements(
                preferred=("reasoning", "long_context"),
                discouraged=("weak_planning",),
            ),
        },
    )


class _FakeProject:
    def __init__(self, key: str, *, name: str | None = None) -> None:
        self.key = key
        self.name = name or key.title()
        self.role_assignments: dict[str, ModelAssignment] = {}


class _FakePollyPM:
    def __init__(self) -> None:
        self.release_channel = "stable"
        self.role_assignments = {
            "architect": ModelAssignment(alias="haiku-4.5"),
            "worker": ModelAssignment(alias="codex-gpt-5.4"),
        }


class _FakeSession:
    def __init__(
        self,
        name: str,
        *,
        project: str,
        provider: ProviderKind,
        account: str,
        args: list[str] | None = None,
    ) -> None:
        self.name = name
        self.role = "worker"
        self.project = project
        self.enabled = True
        self.window_name = f"win-{name}"
        self.cwd = f"/tmp/{name}"
        self.provider = provider
        self.account = account
        self.args = args or ["--flag"]


class _FakeAccount:
    def __init__(self, email: str, provider: ProviderKind) -> None:
        self.email = email
        self.provider = provider


class _FakeUsage:
    def __init__(self, *, used_pct: int, remaining_pct: int, usage_summary: str, updated_at: str) -> None:
        self.used_pct = used_pct
        self.remaining_pct = remaining_pct
        self.usage_summary = usage_summary
        self.updated_at = updated_at


class _FakeConfig:
    def __init__(self) -> None:
        self.pollypm = _FakePollyPM()
        self.projects = {"alpha": _FakeProject("alpha", name="Alpha Project")}
        self.sessions = {
            "worker-alpha": _FakeSession(
                "worker-alpha",
                project="alpha",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                args=["--model", "claude"],
            ),
        }
        self.accounts = {
            "claude_main": _FakeAccount("claude@example.com", ProviderKind.CLAUDE),
            "codex_main": _FakeAccount("codex@example.com", ProviderKind.CODEX),
        }


class _FakeService:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.switched: list[tuple[str, str]] = []

    def stop_session(self, name: str) -> None:
        self.stopped.append(name)

    def switch_session_account(self, session_name: str, account_name: str) -> None:
        self.switched.append((session_name, account_name))


@pytest.fixture
def project_settings_env(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("# test config\n")
    fake_config = _FakeConfig()
    service = _FakeService()
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_config",
        lambda _path: fake_config,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.PollyPMService",
        lambda _path: service,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_cached_account_usage",
        lambda _path: {
            "claude_main": _FakeUsage(
                used_pct=19,
                remaining_pct=81,
                usage_summary="81% left this week",
                updated_at="2026-04-21T09:00:00+00:00",
            ),
            "codex_main": _FakeUsage(
                used_pct=78,
                remaining_pct=22,
                usage_summary="22% left this week",
                updated_at="2026-04-21T09:30:00+00:00",
            ),
        },
    )
    monkeypatch.setattr(
        "pollypm.cockpit_settings_history.Path.home",
        lambda: tmp_path / "home",
    )
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_registry",
        _fake_role_registry,
    )
    from pollypm.cockpit_project_settings import PollyProjectSettingsApp

    return {
        "app": PollyProjectSettingsApp(config_path, "alpha"),
        "config_path": config_path,
        "service": service,
    }


def _run(coro) -> None:
    asyncio.run(coro)


def test_project_settings_reexport_matches_sibling_module() -> None:
    from pollypm.cockpit_project_settings import PollyProjectSettingsApp as direct
    from pollypm.cockpit_ui import PollyProjectSettingsApp as compat

    assert compat is direct


def test_project_settings_renders_worker_and_account(project_settings_env) -> None:
    app = project_settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert "Alpha Project" in str(app.query_one("#title-bar").render())
            worker_info = str(app.query_one("#worker-info").render())
            assert "worker-alpha" in worker_info
            assert "win-worker-alpha" in worker_info
            model_info = str(app.query_one("#model-info").render())
            assert "claude@example.com" in model_info
            assert "claude" in model_info
            preview = str(app.query_one("#preview").render())
            assert "Diff preview" in preview
            assert "Current budget" in preview
            assert "19% used / 81% left" in preview
            assert "Claude target" in preview
            assert "22% left" in preview
            assert "Codex target" in preview
            assert "Default account" in preview

    _run(body())


def test_project_settings_renders_project_role_rows(project_settings_env) -> None:
    app = project_settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            assert app.role_table.row_count == 3
            detail = str(app.query_one("#project-role-detail").render())
            assert "Architect" in detail
            assert "inherited global" in detail
            assert "weak_planning" in detail

    _run(body())


def test_project_settings_reset_and_switch_provider(project_settings_env, monkeypatch) -> None:
    app = project_settings_env["app"]
    service = project_settings_env["service"]

    def _auto_confirm(_screen, callback=None, **_kwargs):
        if callback is not None:
            callback(True)

    monkeypatch.setattr(app, "push_screen", _auto_confirm)

    async def body() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.on_reset(None)
            assert service.stopped == ["worker-alpha"]
            assert "Session worker-alpha stopped" in str(app.query_one("#message").render())
            app.on_switch_codex(None)
            assert service.switched == [("worker-alpha", "codex_main")]
            assert "Switched to codex" in str(app.query_one("#message").render())

        new_app = type(app)(project_settings_env["config_path"], "alpha")
        monkeypatch.setattr(new_app, "push_screen", _auto_confirm)
        async with new_app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            new_app.action_undo_recent_change()
            assert service.switched[-1] == ("worker-alpha", "claude_main")
            assert "Undid" in str(new_app.query_one("#message").render())

    _run(body())


def test_project_settings_preview_uses_latest_history_event(project_settings_env, monkeypatch, tmp_path: Path) -> None:
    from pollypm.cockpit_settings_history import record_settings_history

    monkeypatch.setattr(
        "pollypm.cockpit_settings_history.Path.home",
        lambda: tmp_path / "home",
    )
    record_settings_history(
        "manual_switch",
        "worker-alpha -> codex_main",
        {
            "session_name": "worker-alpha",
            "from_account": "claude_main",
            "to_account": "codex_main",
        },
    )

    app = project_settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            preview = str(app.query_one("#preview").render())
            assert "Recent manual switch" in preview
            assert "codex_main" in preview

    _run(body())


def test_project_settings_buttons_include_inline_key_hints(project_settings_env) -> None:
    app = project_settings_env["app"]

    async def body() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#reset-session", Button).label.plain == "[R] Reset Session"
            assert app.query_one("#switch-claude", Button).label.plain == "[C] Switch to Claude"
            assert app.query_one("#switch-codex", Button).label.plain == "[X] Switch to Codex"
            assert app.query_one("#undo", Button).label.plain == "[U] Undo"

    _run(body())


def test_project_settings_role_editor_persists_override_custom_and_inherit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pollypm.config import load_config, write_config
    from pollypm.models import (
        AccountConfig,
        KnownProject,
        PollyPMConfig,
        PollyPMSettings,
        ProjectKind,
        ProjectSettings,
        SessionConfig,
    )

    config_path = tmp_path / "pollypm.toml"
    project_root = tmp_path / "alpha"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm" / "logs",
            snapshots_dir=tmp_path / ".pollypm" / "snapshots",
            state_db=tmp_path / ".pollypm" / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_primary",
            role_assignments={
                "architect": ModelAssignment(alias="opus-4.7"),
                "worker": ModelAssignment(alias="codex-gpt-5.4"),
            },
        ),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / "homes" / "claude_primary",
            ),
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                home=tmp_path / "homes" / "codex_primary",
            ),
        },
        sessions={
            "worker_alpha": SessionConfig(
                name="worker_alpha",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=project_root,
                project="alpha",
            )
        },
        projects={
            "alpha": KnownProject(
                key="alpha",
                path=project_root,
                kind=ProjectKind.FOLDER,
            ),
        },
    )
    write_config(config, config_path, force=True)

    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_cached_account_usage",
        lambda _path: {},
    )
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_registry",
        _fake_role_registry,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_settings_history.Path.home",
        lambda: tmp_path / "home",
    )

    from pollypm.cockpit_project_settings import PollyProjectSettingsApp

    app = PollyProjectSettingsApp(config_path, "alpha")

    async def body() -> None:
        async with app.run_test(size=(150, 38)) as pilot:
            await pilot.pause()
            app.role_table.move_cursor(row=0)
            await pilot.pause()
            app._save_project_role_assignment("architect", ModelAssignment(alias="sonnet-4.6"))
            await pilot.pause()
            reloaded = load_config(config_path)
            assert reloaded.projects["alpha"].role_assignments["architect"] == ModelAssignment(
                alias="sonnet-4.6"
            )

            app.role_provider_input.value = "codex"
            await pilot.pause()
            app.role_model_input.value = "gpt-5.4-custom"
            app._save_project_role_assignment(
                "architect",
                ModelAssignment(provider="codex", model="gpt-5.4-custom"),
            )
            await pilot.pause()
            reloaded = load_config(config_path)
            assert reloaded.projects["alpha"].role_assignments["architect"] == ModelAssignment(
                provider="codex",
                model="gpt-5.4-custom",
            )

            app._clear_project_role_override("architect")
            await pilot.pause()
            reloaded = load_config(config_path)
            assert "architect" not in reloaded.projects["alpha"].role_assignments
            detail = str(app.query_one("#project-role-detail").render())
            assert "inherited global" in detail

    _run(body())


def test_project_settings_release_channel_picker_persists(tmp_path: Path, monkeypatch) -> None:
    """Changing the Release channel radio writes the selection to config."""
    from textual.widgets import RadioButton

    from pollypm.config import load_config, write_config
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

    # Build a minimal real config so ``load_config`` / ``write_config``
    # round-trip exercises the actual parse path — this is the
    # integration we care about for the picker.
    config_path = tmp_path / "pollypm.toml"
    base_dir = tmp_path
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=base_dir,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=base_dir / "homes" / "claude_primary",
            )
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=base_dir,
            ),
            "worker_alpha": SessionConfig(
                name="worker_alpha",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=base_dir,
                project="alpha",
            ),
        },
        projects={
            "alpha": KnownProject(
                key="alpha",
                path=base_dir,
                name="Alpha",
                kind=ProjectKind.FOLDER,
            ),
        },
    )
    write_config(config, config_path, force=True)
    assert load_config(config_path).pollypm.release_channel == "stable"

    # Point the cache-invalidation target at an isolated home so the
    # unlink stays sandboxed.
    fake_home = tmp_path / "home"
    (fake_home / ".pollypm").mkdir(parents=True)
    cache_path = fake_home / ".pollypm" / "release-check.json"
    cache_path.write_text("{}")
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.Path.home",
        lambda: fake_home,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_project_settings.load_registry",
        _fake_role_registry,
    )

    from pollypm.cockpit_project_settings import PollyProjectSettingsApp

    app = PollyProjectSettingsApp(config_path, "alpha")

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            beta = app.query_one("#release-channel-beta", RadioButton)
            beta.value = True
            await pilot.pause()

    _run(body())

    reloaded = load_config(config_path)
    assert reloaded.pollypm.release_channel == "beta"
    assert not cache_path.exists()
