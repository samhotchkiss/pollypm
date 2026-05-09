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
