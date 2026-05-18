from pathlib import Path


def test_initialize_plugin_host_for_pane_registers_activity_projector(
    tmp_path: Path,
) -> None:
    """#1694 — a standalone ``pm cockpit-pane`` process runs in its own
    Python process, so the per-process registration seam in
    :mod:`pollypm.activity_projector_registry` starts empty. The
    cockpit-pane CLI helper must initialize the plugin host so the
    built-in ``activity_feed`` plugin populates the registry; otherwise
    standalone activity / project panes render an empty feed even when
    the plugin is installed.
    """
    from pollypm import activity_projector_registry
    from pollypm.cli_features.ui import _initialize_plugin_host_for_pane

    # Make a config that resolves a real ``root_dir`` so ``ExtensionHost``
    # can find the built-in plugin tree (it's keyed off ``root_dir``).
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        '[project]\nname = "sample"\n\n[pollypm]\ncontroller_account = ""\n'
    )

    # Clear the registry to simulate a cold cockpit-pane process —
    # ``register_activity_projector_factory(None)`` is the documented
    # test hook for "plugin not yet initialized in this process".
    activity_projector_registry.register_activity_projector_factory(None)
    assert activity_projector_registry.get_activity_projector_factory() is None

    _initialize_plugin_host_for_pane(config_path)

    assert activity_projector_registry.get_activity_projector_factory() is not None, (
        "standalone cockpit-pane init must register the activity-projector "
        "factory (#1694) — otherwise the activity feed renders empty"
    )


def test_initialize_plugin_host_for_pane_swallows_config_load_failure(
    tmp_path: Path,
) -> None:
    """The helper must never block the pane from launching — config load
    failures and plugin init exceptions are best-effort, matching the
    cockpit rail's tolerance. See ``cockpit_rail._rail_registry``.
    """
    from pollypm.cli_features.ui import _initialize_plugin_host_for_pane

    missing_config = tmp_path / "does_not_exist.toml"
    # No assertion needed: the contract is "must not raise".
    _initialize_plugin_host_for_pane(missing_config)


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
