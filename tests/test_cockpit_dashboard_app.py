from pathlib import Path


def test_cockpit_ui_reexports_dashboard_app() -> None:
    from pollypm.cockpit_apps.dashboard import PollyDashboardApp as DirectDashboardApp
    from pollypm.cockpit_ui import PollyDashboardApp as CompatDashboardApp

    assert CompatDashboardApp is DirectDashboardApp


def test_dashboard_app_jump_inbox_uses_navigation_client(monkeypatch, tmp_path: Path) -> None:
    from pollypm.cockpit_apps import dashboard

    config_path = tmp_path / "pollypm.toml"
    calls: list[tuple[Path, str] | str] = []

    class FakeNavigationClient:
        def jump_to_inbox(self) -> None:
            calls.append("jump_to_inbox")

    def fake_file_navigation_client(
        actual_config_path: Path,
        *,
        client_id: str,
    ) -> FakeNavigationClient:
        calls.append((actual_config_path, client_id))
        return FakeNavigationClient()

    monkeypatch.setattr(
        dashboard, "file_navigation_client", fake_file_navigation_client
    )

    app = dashboard.PollyDashboardApp(config_path)
    app._route_to_inbox()

    assert calls == [(config_path, "polly-dashboard"), "jump_to_inbox"]
