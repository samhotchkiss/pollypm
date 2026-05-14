"""Regression tests for project-dashboard key resolution (#1544).

Background — pre-#1544 ``_gather_project_dashboard`` did a single
``config.projects.get(project_key)`` lookup. The rail label can carry
a hyphenated form (``health-coach``) while the canonical config key
is the underscore-slugified variant (``health_coach``); the user
typing ``pm cockpit-pane project health-coach`` then bounced to a
"is not a tracked project — open the project picker" topbar with an
empty banner and an empty Inbox panel.

That copy is "internal vocabulary" (``tracked``) plus a non-existent
affordance (``project picker``). Witness symptom (#1544): the rail
listed ``health-coach`` with the active heart-with-dot glyph, the user
clicked it, and the dashboard refused to render. Two surfaces
contradicted on the same project key.

These tests pin two contracts:

1. ``_resolve_project_key`` accepts the rail's hyphenated form and
   resolves it to the canonical underscored config key.
2. ``_resolve_project_key`` is also case-insensitive, catching
   operator typos like ``Coffeeboardnm`` for ``coffeeboardnm``.
3. The dashboard's bail-state copy no longer says "tracked project"
   or "project picker" (#1544 explicitly called those out as
   internal-vocabulary failures with no real action).
"""

from __future__ import annotations

from types import SimpleNamespace

from pollypm.cockpit_ui import (
    PollyProjectDashboardApp,
    _resolve_project_key,
)


def _config_with_projects(*keys: str) -> SimpleNamespace:
    """Return a config-like object exposing ``projects`` as a dict."""
    return SimpleNamespace(
        projects={
            key: SimpleNamespace(key=key, name=key, tracked=True)
            for key in keys
        },
    )


def test_resolve_project_key_returns_direct_match() -> None:
    config = _config_with_projects("coffeeboardnm", "media")
    resolved = _resolve_project_key(config, "media")
    assert resolved is not None
    canonical, project = resolved
    assert canonical == "media"
    assert project.key == "media"


def test_resolve_project_key_normalizes_hyphen_to_underscore() -> None:
    """The rail label can carry hyphens (``health-coach``) while the
    config key is the underscore-slugified form (``health_coach``).
    """
    config = _config_with_projects("health_coach", "polly_remote")
    resolved = _resolve_project_key(config, "health-coach")
    assert resolved is not None
    canonical, project = resolved
    assert canonical == "health_coach"
    assert project.key == "health_coach"


def test_resolve_project_key_is_case_insensitive() -> None:
    """Operator types ``Coffeeboardnm`` for the registered ``coffeeboardnm``."""
    config = _config_with_projects("coffeeboardnm")
    resolved = _resolve_project_key(config, "Coffeeboardnm")
    assert resolved is not None
    canonical, _project = resolved
    assert canonical == "coffeeboardnm"


def test_resolve_project_key_combines_slug_and_case() -> None:
    """Operator types ``Health-Coach`` for the registered ``health_coach``."""
    config = _config_with_projects("health_coach")
    resolved = _resolve_project_key(config, "Health-Coach")
    assert resolved is not None
    canonical, _project = resolved
    assert canonical == "health_coach"


def test_resolve_project_key_returns_none_for_unknown() -> None:
    """A genuinely-missing key still resolves to None — the dashboard
    can then fall through to its bail-state topbar copy."""
    config = _config_with_projects("coffeeboardnm")
    assert _resolve_project_key(config, "ghost-project") is None


def test_resolve_project_key_handles_empty_config() -> None:
    config = SimpleNamespace(projects={})
    assert _resolve_project_key(config, "anything") is None
    config_no_attr = SimpleNamespace()
    assert _resolve_project_key(config_no_attr, "anything") is None


def _render_bail_topbar() -> str:
    """Mount the dashboard app with a missing project key and capture
    the bail-state topbar markup the user actually sees.

    The render helper bypasses ``__init__`` (which boots a Textual
    pipeline + worker thread) and only invokes ``_render`` against a
    captured topbar mock — enough to read out what the user would see
    on the failing surface, without any of the surrounding I/O.
    """
    captured: list[str] = []

    class _TopbarStub:
        def update(self, value: str) -> None:
            captured.append(str(value))

    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    app.data = None
    app.project_key = "ghost-project"
    app.topbar = _TopbarStub()
    PollyProjectDashboardApp._render(app)
    assert captured, "expected the bail branch to push at least one update"
    return captured[-1]


def test_dashboard_bail_copy_no_longer_uses_internal_vocabulary() -> None:
    """#1544 — the bail-state topbar must NOT use the words ``tracked
    project`` or ``project picker``. Both were called out in the
    issue as internal-data-model jargon plus a non-existent affordance.

    We render the bail topbar and assert against the rendered markup so
    the test doesn't false-fail on comment text that mentions the
    removed phrases.
    """
    bail_text = _render_bail_topbar()
    assert "tracked project" not in bail_text, (
        "Bail-state copy must not use the internal-vocabulary phrase "
        "``tracked project`` (#1544). The user has no way to know what "
        "tracked vs untracked means."
    )
    assert "project picker" not in bail_text, (
        "Bail-state copy must not point users at the non-existent "
        "``project picker`` affordance (#1544)."
    )


def test_dashboard_bail_copy_names_a_real_command() -> None:
    """The replacement copy must point users at a CLI command they can
    actually run (``pm project new``) — Sam's pattern: never hand the
    user a dead-end."""
    bail_text = _render_bail_topbar()
    assert "pm project new" in bail_text, (
        "Bail-state copy must name ``pm project new`` (the real "
        "registration command) so the user has a concrete next action."
    )


# ---------------------------------------------------------------------------
# Codex review of PR #1557 — hyphen-input → canonical key navigation
# ---------------------------------------------------------------------------


def _stub_dashboard_app(initial_key: str) -> PollyProjectDashboardApp:
    """Mount a ``PollyProjectDashboardApp`` shell without booting Textual.

    ``__init__`` boots a Textual pipeline + worker thread which is
    way too heavy for a routing assertion. We bypass it and only set
    the attributes ``_first_refresh_completed`` reads.
    """
    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    app.project_key = initial_key
    app.data = None
    app._first_refresh_running = True
    app._last_render_signature = None

    # ``_first_refresh_completed`` calls ``self._render`` at the end —
    # stub it out so we don't drag the real render path (which expects
    # mounted Textual widgets) into a unit test.
    app._render = lambda: None  # type: ignore[method-assign]
    return app


def _data_stub(project_key: str) -> SimpleNamespace:
    """Return a ``ProjectDashboardData``-shaped namespace populated with
    just enough fields for ``_project_dashboard_signature`` to consume
    without raising. Only ``project_key`` is meaningful for the routing
    assertion — every other field is a benign default.
    """
    return SimpleNamespace(
        project_key=project_key,
        pm_label="",
        exists_on_disk=True,
        status_dot="",
        status_label="",
        active_worker=None,
        task_counts={},
        inbox_count=0,
        alert_count=0,
        alert_types=[],
        action_items=[],
        plan_path=None,
        plan_mtime=None,
        plan_stale_reason=None,
        plan_task_summary=None,
        activity_entries=[],
    )


def test_first_refresh_adopts_canonical_key_after_resolver_runs() -> None:
    """Codex review (PR #1557) — when the user types a hyphenated key
    (``health-coach``) at the rail and the gather resolves it to the
    canonical config key (``health_coach``), the dashboard app MUST
    update ``self.project_key`` to the canonical form.

    Pre-fix the data was rendered under the canonical key but the app's
    ``self.project_key`` stayed on the original input. Follow-on actions
    (PM chat dispatch at ``c``, inbox jump at ``i``, task jumps) all
    route through ``self.project_key``, so the user landed on the
    canonical surface but every keystroke routed through the unregistered
    hyphen form.
    """
    app = _stub_dashboard_app(initial_key="health-coach")
    # Stand-in for ``ProjectDashboardData`` — the only attribute the
    # canonical-key adoption path reads is ``project_key``; the rest
    # exist so ``_project_dashboard_signature`` can run cleanly.
    data = _data_stub("health_coach")

    PollyProjectDashboardApp._first_refresh_completed(app, data)

    assert app.project_key == "health_coach", (
        "expected ``self.project_key`` to adopt the canonical key "
        "``health_coach`` once the gather resolved the hyphenated "
        f"input; got {app.project_key!r}"
    )


def test_first_refresh_completed_routes_inbox_through_canonical_key() -> None:
    """Codex review (PR #1557) — after the dashboard adopts the canonical
    key, the inbox-jump action (``i``) must route through the canonical
    form (``health_coach``), not the original hyphenated input.

    We capture the argument passed to ``jump_to_inbox`` via a monkeypatched
    navigation client so the assertion is on the actual routing call,
    not on the adopted attribute alone.
    """
    import pollypm.cockpit_ui as _cockpit_ui

    captured: dict[str, str] = {}

    class _ClientStub:
        def jump_to_inbox(self, project_key: str) -> None:
            captured["project_key"] = project_key

    app = _stub_dashboard_app(initial_key="health-coach")
    data = _data_stub("health_coach")
    PollyProjectDashboardApp._first_refresh_completed(app, data)

    app.config_path = None  # type: ignore[assignment]

    original = _cockpit_ui.file_navigation_client
    _cockpit_ui.file_navigation_client = lambda *a, **kw: _ClientStub()
    try:
        PollyProjectDashboardApp._route_to_inbox(app)
    finally:
        _cockpit_ui.file_navigation_client = original

    assert captured.get("project_key") == "health_coach", (
        "expected ``i`` (jump to inbox) to route through the canonical "
        f"key after resolution; got {captured!r}"
    )


def test_first_refresh_completed_routes_tasks_through_canonical_key() -> None:
    """Codex review (PR #1557) — task-list jumps must also route through
    the canonical key after adoption. ``pm cockpit-pane project
    health-coach`` → press ``→`` to drill into tasks → ``jump_to_project``
    is called with ``health_coach``, not the original hyphen form.
    """
    import pollypm.cockpit_ui as _cockpit_ui

    captured: dict[str, object] = {}

    class _ClientStub:
        def jump_to_project(self, project_key: str, *, view: str) -> None:
            captured["project_key"] = project_key
            captured["view"] = view

    app = _stub_dashboard_app(initial_key="health-coach")
    data = _data_stub("health_coach")
    PollyProjectDashboardApp._first_refresh_completed(app, data)
    app.config_path = None  # type: ignore[assignment]

    original = _cockpit_ui.file_navigation_client
    _cockpit_ui.file_navigation_client = lambda *a, **kw: _ClientStub()
    try:
        PollyProjectDashboardApp._route_to_tasks(app)
    finally:
        _cockpit_ui.file_navigation_client = original

    assert captured.get("project_key") == "health_coach", (
        "expected the tasks-jump to route through the canonical key "
        f"after resolution; got {captured!r}"
    )
    assert captured.get("view") == "issues"


def test_first_refresh_keeps_input_when_data_is_none() -> None:
    """If the gather returned ``None`` (project genuinely missing),
    the app must NOT clobber ``self.project_key`` with ``None`` — the
    bail-state render path still needs the original input to surface
    ``"X" is not registered`` copy.
    """
    app = _stub_dashboard_app(initial_key="ghost-project")
    PollyProjectDashboardApp._first_refresh_completed(app, None)
    assert app.project_key == "ghost-project"
