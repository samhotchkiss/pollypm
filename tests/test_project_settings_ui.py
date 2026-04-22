"""Focused tests for :class:`pollypm.cockpit_project_settings.PollyProjectSettingsApp`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from textual.widgets import Button

from pollypm.models import ProviderKind


class _FakeProject:
    def __init__(self, key: str, *, name: str | None = None) -> None:
        self.key = key
        self.name = name or key.title()


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


class _FakeConfig:
    def __init__(self) -> None:
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
        "pollypm.cockpit_settings_history.Path.home",
        lambda: tmp_path / "home",
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
            assert "Undo" in preview or "undo" in preview.lower()

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
