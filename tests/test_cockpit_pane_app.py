from pathlib import Path


def test_cockpit_ui_reexports_pane_app() -> None:
    from pollypm.cockpit_apps.pane import PollyCockpitPaneApp as DirectPaneApp
    from pollypm.cockpit_ui import PollyCockpitPaneApp as CompatPaneApp

    assert CompatPaneApp is DirectPaneApp


def test_pane_app_refresh_uses_cockpit_detail(monkeypatch, tmp_path: Path) -> None:
    from pollypm.cockpit_apps import pane

    config_path = tmp_path / "pollypm.toml"
    calls: list[tuple[Path, str, str | None]] = []

    def fake_build_cockpit_detail(
        actual_config_path: Path,
        kind: str,
        target: str | None,
    ) -> str:
        calls.append((actual_config_path, kind, target))
        return "pane content"

    monkeypatch.setattr(pane, "build_cockpit_detail", fake_build_cockpit_detail)

    app = pane.PollyCockpitPaneApp(config_path, "dashboard", "demo")
    app._refresh()

    assert calls == [(config_path, "dashboard", "demo")]
    assert app.body.content == "pane content"
