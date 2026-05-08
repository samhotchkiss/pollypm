"""Tests for er02 — core_rail_items built-in plugin + registry-driven rail.

See docs/extensible-rail-spec.md §5 and issue #222.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pollypm.cockpit_live_chat_notice import (
    LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY,
    LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY,
    LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE,
)
from pollypm.cockpit_rail import CockpitRouter
from pollypm.models import KnownProject, ProjectKind
from pollypm.plugin_api.v1 import (
    PanelSpec,
    PollyPMPlugin,
    RailContext,
    RailRegistry,
)
from pollypm.plugin_host import ExtensionHost
from pollypm.plugins_builtin.core_rail_items import plugin as core_rail_items_plugin


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        f"root_dir = \"{tmp_path}\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    return config_path


class _FakeConfig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        sessions: dict | None = None,
        projects: dict | None = None,
    ) -> None:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        default_projects = {
            "pollypm": KnownProject(
                key="pollypm", path=tmp_path, name="PollyPM",
                persona_name="Pete", kind=ProjectKind.GIT,
            ),
            "demo": KnownProject(
                key="demo", path=tmp_path / "demo", name="Demo",
                persona_name="Dora", kind=ProjectKind.GIT,
            ),
        }
        self.project = Project()
        self.projects = default_projects if projects is None else projects
        # ``sessions`` is read by visibility predicates that gate rail
        # entries on whether a backing ``[sessions.<name>]`` block
        # exists (#962 — Russell · chat). Default to operator + reviewer
        # configured so legacy tests that don't care about visibility
        # keep their previous shape.
        self.sessions = sessions if sessions is not None else {
            "operator": object(),
            "reviewer": object(),
        }


class _FakeLaunch:
    def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
        self.window_name = window_name
        self.session = type(
            "Session", (),
            {
                "name": name, "role": role, "project": project,
                "provider": type("P", (), {"value": "claude"})(),
            },
        )()


class _FakeWindow:
    def __init__(self, name: str, pane_dead: bool = False) -> None:
        self.name = name
        self.pane_dead = pane_dead
        self.pane_id = f"%{name}"


def test_polly_state_surfaces_live_chat_network_dead_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_rail_items_plugin,
        "_session_state",
        lambda *_args, **_kwargs: "ready",
    )
    ctx = RailContext(
        cockpit_state={
            LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY: (
                LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE
            ),
            LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY: time.time() + 30,
        },
    )

    assert core_rail_items_plugin._polly_state(ctx) == (
        f"! {LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE}"
    )


def test_polly_state_ignores_expired_live_chat_network_dead_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_rail_items_plugin,
        "_session_state",
        lambda *_args, **_kwargs: "ready",
    )
    ctx = RailContext(
        cockpit_state={
            LIVE_CHAT_NETWORK_DEAD_NOTICE_MESSAGE_KEY: (
                LIVE_CHAT_NETWORK_DEAD_RAIL_MESSAGE
            ),
            LIVE_CHAT_NETWORK_DEAD_NOTICE_UNTIL_KEY: time.time() - 1,
        },
    )

    assert core_rail_items_plugin._polly_state(ctx) == "ready"


@pytest.mark.parametrize(
    ("session_role", "expected_label"),
    [
        ("architect", "  PM Chat (Archie)"),
        ("worker", "  PM Chat (Bea)"),
    ],
)
def test_pm_chat_label_matches_project_session_persona(
    monkeypatch, tmp_path: Path, session_role: str, expected_label: str,
) -> None:
    project = KnownProject(
        key="bikepath",
        path=tmp_path,
        name="Bikepath",
        persona_name="Bea",
        kind=ProjectKind.GIT,
    )
    config = type("Config", (), {"projects": {"bikepath": project}})()

    class _Router:
        def _session_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return "idle"

    monkeypatch.setattr(
        core_rail_items_plugin,
        "_classify_projects",
        lambda ctx: ([("bikepath", project)], [], {"bikepath": False}),
    )
    monkeypatch.setattr(
        core_rail_items_plugin,
        "active_task_numbers",
        lambda project, *, config=None: [],
    )
    ctx = RailContext(
        router=_Router(),
        config=config,
        launches=[
            _FakeLaunch(
                f"{session_role}_bikepath",
                session_role,
                "bikepath",
                f"{session_role}-bikepath",
            ),
        ],
        cockpit_state={"selected": "project:bikepath"},
    )

    rows = core_rail_items_plugin._project_rows(ctx)

    session_row = next(row for row in rows if row.key == "project:bikepath:session")
    assert session_row.label == expected_label


def test_pm_chat_label_uses_project_pm_fallback(monkeypatch, tmp_path: Path) -> None:
    project = KnownProject(
        key="booktalk",
        path=tmp_path,
        name="Booktalk",
        persona_name=None,
        kind=ProjectKind.GIT,
    )
    config = type("Config", (), {"projects": {"booktalk": project}})()

    class _Router:
        def _session_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return "idle"

    monkeypatch.setattr(
        core_rail_items_plugin,
        "_classify_projects",
        lambda ctx: ([("booktalk", project)], [], {"booktalk": False}),
    )
    monkeypatch.setattr(
        core_rail_items_plugin,
        "active_task_numbers",
        lambda project, *, config=None: [],
    )
    ctx = RailContext(
        router=_Router(),
        config=config,
        launches=[],
        cockpit_state={"selected": "project:booktalk"},
    )

    rows = core_rail_items_plugin._project_rows(ctx)

    session_row = next(row for row in rows if row.key == "project:booktalk:session")
    assert session_row.label == "  PM Chat (Project PM)"


def _fake_supervisor(
    tmp_path: Path,
    *,
    sessions: dict | None = None,
    projects: dict | None = None,
):
    config = _FakeConfig(tmp_path, sessions=sessions, projects=projects)

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = config

        def status(self):
            launches = [
                _FakeLaunch("operator", "operator-pm", "pollypm", "pm-operator"),
                _FakeLaunch("worker_demo", "worker", "demo", "worker-demo"),
            ]
            windows = [_FakeWindow("pm-operator"), _FakeWindow("worker-demo")]
            return launches, windows, [], [], []

    return FakeSupervisor()


def test_core_rail_items_plugin_loads_from_builtin_dir() -> None:
    """The core_rail_items plugin must be discoverable via the default
    builtin search path (no project-local manifest required)."""
    host = ExtensionHost(Path("/tmp"))
    plugins = host.plugins()
    assert "core_rail_items" in plugins


def test_build_items_identical_to_legacy_shape(monkeypatch, tmp_path: Path) -> None:
    """Visual-parity test — rail items match the canonical shape.

    #870 added a separate ``dashboard`` ("Home") row at the top so the
    "Polly · chat" live pane is distinct from the static dashboard
    surface — users were opening Polly expecting the dashboard. The
    canonical top-section therefore starts with ``dashboard`` followed
    by the chat / inbox / workers / metrics rows, then projects, then
    settings last.

    The test asserts structural ordering (dashboard first, settings
    last, polly+russell+inbox sit before project rows) without pinning
    a specific list-of-keys snapshot — the rail registry intentionally
    grows as plugins land, and this test should not become a barrier
    to that.
    """
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 1,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: _fake_supervisor(tmp_path))

    items = router.build_items(spinner_index=2)
    keys = [item.key for item in items]

    # Core rail entries all present.
    assert "dashboard" in keys
    assert "polly" in keys
    assert "russell" in keys
    assert "inbox" in keys
    assert "project:pollypm" in keys
    assert "project:demo" in keys
    assert "settings" in keys

    # Order: dashboard first, settings last.
    assert items[0].key == "dashboard"
    assert items[-1].key == "settings"

    # Top section sits before any project row.
    project_indices = [i for i, item in enumerate(items) if item.key.startswith("project:")]
    first_project = min(project_indices)
    for top_key in ("dashboard", "polly", "russell", "inbox"):
        assert keys.index(top_key) < first_project, (
            f"{top_key!r} must precede project rows; "
            f"got order {keys}"
        )

    # Inbox count surfaces in its label (drift signal that
    # _count_inbox_tasks_for_label monkey-patch reached the rail).
    inbox_item = next(item for item in items if item.key == "inbox")
    assert inbox_item.label == "Inbox (1)"


def test_build_items_keeps_projects_section_when_empty(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(
        router,
        "_load_supervisor",
        lambda: _fake_supervisor(tmp_path, projects={}),
    )

    items = router.build_items(spinner_index=0)
    keys = [item.key for item in items]
    project_section = next(item for item in items if item.key == "projects_root")

    assert project_section.label == "Projects"
    assert project_section.selectable is False
    assert keys.index("inbox") < keys.index("projects_root") < keys.index("settings")


def test_russell_rail_entry_hidden_when_reviewer_session_unconfigured(
    monkeypatch, tmp_path: Path,
) -> None:
    """#962 — clicking ``Russell · chat`` silently routed to the Dashboard
    when no ``[sessions.reviewer]`` block existed: the live-session
    lookup raised, ``_route_live_session`` caught the exception, and
    fell through to the static fallback (``polly`` → Dashboard).

    The fix hides the rail entry when no reviewer session is
    configured so the rail-entry-to-route contract holds: every
    visible entry must resolve to a non-default destination. This
    test pins that contract — no ``reviewer`` config, no rail row.
    """
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    # Operator alone — reviewer block is missing (matches the user
    # config that surfaced the bug in #962).
    monkeypatch.setattr(
        router,
        "_load_supervisor",
        lambda: _fake_supervisor(tmp_path, sessions={"operator": object()}),
    )

    items = router.build_items(spinner_index=0)
    keys = [item.key for item in items]

    assert "polly" in keys, "operator session is configured, polly row must render"
    assert "russell" not in keys, (
        "reviewer session is unconfigured — Russell · chat must be hidden "
        "instead of dead-ending on the Dashboard (#962)"
    )


def test_russell_rail_entry_visible_when_reviewer_session_configured(
    monkeypatch, tmp_path: Path,
) -> None:
    """Sibling to the previous test — when a ``[sessions.reviewer]``
    block does exist, the rail entry must surface so the user can jump
    to Russell's chat pane (the originally-intended UX)."""
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(
        router,
        "_load_supervisor",
        lambda: _fake_supervisor(
            tmp_path,
            sessions={"operator": object(), "reviewer": object()},
        ),
    )

    items = router.build_items(spinner_index=0)
    keys = [item.key for item in items]

    assert "russell" in keys


def test_build_items_preserves_working_project_session_state(
    monkeypatch, tmp_path: Path,
) -> None:
    """Working sessions must not silently fall back to idle in rail rows."""
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: _fake_supervisor(tmp_path))
    monkeypatch.setattr(
        router,
        "_is_pane_working",
        lambda window, provider, *, heartbeat=None, session_name=None: True,
    )

    items = router.build_items(spinner_index=1)
    demo_item = next(item for item in items if item.key == "project:demo")

    assert demo_item.state.endswith("working")
    assert demo_item.state != "idle"


def test_is_pane_working_returns_false_when_snapshot_stable(
    monkeypatch, tmp_path: Path,
) -> None:
    """#764: if the session's last three heartbeats all share the same
    snapshot_hash, the pane has not actually produced output — return
    False even if the captured pane text contains "esc to interrupt"
    (claude's turn-in-progress marker).
    """
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")

    from types import SimpleNamespace

    class _StubStore:
        def recent_heartbeats(self, session_name, limit):
            # Three identical snapshot hashes → stable.
            return [
                SimpleNamespace(snapshot_hash="abc123"),
                SimpleNamespace(snapshot_hash="abc123"),
                SimpleNamespace(snapshot_hash="abc123"),
            ]

    class _StubSup:
        store = _StubStore()

    monkeypatch.setattr(router, "_load_supervisor", lambda: _StubSup())
    assert router._session_snapshot_is_stable("demo_worker") is True

    window = _FakeWindow("worker-demo")
    provider = SimpleNamespace(value="claude")
    # Even though the pane text DOES contain the working marker, the
    # stability check short-circuits and reports "not working."
    monkeypatch.setattr(
        router.tmux,
        "capture_pane",
        lambda pane_id, lines=15: "some output\nesc to interrupt\n",
        raising=False,
    )
    working = router._is_pane_working(
        window, provider, heartbeat=None, session_name="demo_worker",
    )
    assert working is False


def test_is_pane_working_still_true_when_snapshots_differ(
    monkeypatch, tmp_path: Path,
) -> None:
    """When the pane IS churning (three distinct snapshot hashes across
    the last three heartbeats), the 'esc to interrupt' marker still
    means 'working'. Only unchanged panes are demoted to idle."""
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")

    from types import SimpleNamespace

    class _StubStore:
        def recent_heartbeats(self, session_name, limit):
            return [
                SimpleNamespace(snapshot_hash="a"),
                SimpleNamespace(snapshot_hash="b"),
                SimpleNamespace(snapshot_hash="c"),
            ]

    class _StubSup:
        store = _StubStore()

    monkeypatch.setattr(router, "_load_supervisor", lambda: _StubSup())
    assert router._session_snapshot_is_stable("demo_worker") is False

    window = _FakeWindow("worker-demo")
    provider = SimpleNamespace(value="claude")
    monkeypatch.setattr(
        router.tmux,
        "capture_pane",
        lambda pane_id, lines=15: "fresh turn\nesc to interrupt\n",
        raising=False,
    )
    working = router._is_pane_working(
        window, provider, heartbeat=None, session_name="demo_worker",
    )
    assert working is True


def test_removing_core_rail_items_yields_empty_rail(monkeypatch, tmp_path: Path) -> None:
    """Acceptance: if `core_rail_items` is removed, the rail is empty.

    The activity_feed plugin also registers a rail entry (lf03) — we
    disable it here too so the assertion still isolates the effect of
    removing the core items.
    """
    # Simulate the plugins being disabled by config.
    host = ExtensionHost(tmp_path, disabled=("core_rail_items", "activity_feed"))
    assert "core_rail_items" not in host.plugins()

    monkeypatch.setattr(
        "pollypm.plugin_host.extension_host_for_root", lambda root: host,
    )
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: _fake_supervisor(tmp_path))

    items = router.build_items(spinner_index=0)
    # Rail should be completely empty.
    assert items == []


def test_core_rail_items_surfaces_router_errors_when_strict(monkeypatch, tmp_path: Path) -> None:
    class ExplodingRouter:
        def _session_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("boom")

    ctx = RailContext(extras={"router": ExplodingRouter()})
    monkeypatch.setenv("POLLYPM_STRICT_RAIL_ERRORS", "1")

    with pytest.raises(RuntimeError, match="boom"):
        core_rail_items_plugin._session_state(ctx, "operator")


def test_third_party_plugin_registers_below_core(monkeypatch, tmp_path: Path) -> None:
    """A third-party plugin registering at workflows:150 appears after core items."""
    # Create a plugin that adds a workflows:150 item.
    plugin_dir = tmp_path / "plugins_dir" / "thirdparty"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        """
api_version = "1"
name = "thirdparty"
kind = "rail"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "third-party rail registration"
"""
    )
    (plugin_dir / "plugin.py").write_text(
        """
from pollypm.plugin_api.v1 import PollyPMPlugin, PanelSpec


def _handler(ctx):
    return PanelSpec(widget=None)


def _init(api):
    api.rail.register_item(
        section="workflows",
        index=150,
        label="ThirdParty",
        handler=_handler,
        key="tp",
    )


plugin = PollyPMPlugin(name="thirdparty", initialize=_init)
"""
    )

    # Build host that includes both builtin (to get core_rail_items) and the
    # third-party directory.
    host = ExtensionHost(tmp_path)
    builtin_path = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    host._plugin_search_paths = lambda: [  # type: ignore[assignment]
        ("builtin", builtin_path),
        ("project", tmp_path / "plugins_dir"),
    ]
    host.initialize_plugins(config=_FakeConfig(tmp_path))

    items = host.rail_registry().items()
    # Extract (section, plugin_name, label) in render order.
    order = [(r.section, r.plugin_name, r.label) for r in items]
    # Third-party appears in workflows section (the only workflows entry).
    assert ("workflows", "thirdparty", "ThirdParty") in order

    # Within the top section, core items have low indexes (0, 10, 20) so
    # they render before any plugin-contributed items at index 100+.
    for idx, (section, plugin, _label) in enumerate(order):
        if section == "workflows" and plugin == "thirdparty":
            # Every earlier item is either core (index < 100) or from a
            # different section appearing earlier in RAIL_SECTIONS order.
            for earlier in order[:idx]:
                e_section, e_plugin, _e_label = earlier
                if e_section == "workflows":
                    # Any earlier workflow item must be a core
                    # registration (e.g. activity_feed at index 30) or
                    # the third-party item itself. Plugin-contributed
                    # items at 100+ must always sort after thirdparty
                    # (150) only if their index is higher.
                    assert e_plugin in {
                        "core_rail_items",
                        "activity_feed",
                        "thirdparty",
                    }


def test_rail_registry_items_honour_index_and_section_order() -> None:
    """Independently verify RailRegistry ordering semantics."""
    registry = RailRegistry()
    from pollypm.plugin_api.v1 import RailAPI

    def _h(ctx):
        return PanelSpec(widget=None)

    RailAPI(plugin_name="z", registry=registry).register_item(
        section="workflows", index=50, label="Z50", handler=_h,
    )
    RailAPI(plugin_name="a", registry=registry).register_item(
        section="workflows", index=50, label="A50", handler=_h,
    )
    RailAPI(plugin_name="any", registry=registry, reserved_allowed=True).register_item(
        section="top", index=0, label="Top0", handler=_h,
    )
    RailAPI(plugin_name="t", registry=registry).register_item(
        section="tools", index=0, label="Tool0", handler=_h,
    )
    order = [(r.section, r.label) for r in registry.items()]
    assert order == [
        ("top", "Top0"),
        ("workflows", "A50"),
        ("workflows", "Z50"),
        ("tools", "Tool0"),
    ]


def test_user_waiting_task_ids_skips_non_tracked_projects(tmp_path: Path) -> None:
    """Cycle 91: ``_user_waiting_task_ids`` (used by core_rail_items to
    suppress redundant ``stuck_on_task`` rail glyphs) iterates
    config.projects and opens each project's state.db. Without a
    tracked filter, a registered-but-not-tracked project's leftover
    state.db could over-suppress alerts on tasks that share a number
    with a stale row.

    Same shape as cycles 85/86/87. The function's docstring already
    promised "tracked project's state.db".
    """
    import sqlite3

    from pollypm.plugins_builtin.core_rail_items.plugin import (
        _user_waiting_task_ids,
    )

    def _seed(db_path: Path, project: str, number: int) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE work_tasks ("
                "project TEXT, task_number INTEGER, work_status TEXT)"
            )
            conn.execute(
                "INSERT INTO work_tasks VALUES (?, ?, ?)",
                (project, number, "blocked"),
            )
            conn.commit()
        finally:
            conn.close()

    tracked_path = tmp_path / "tracked"
    _seed(tracked_path / ".pollypm" / "state.db", "tracked", 1)

    ghost_path = tmp_path / "ghost"
    _seed(ghost_path / ".pollypm" / "state.db", "ghost", 99)

    config = type("C", (), {
        "projects": {
            "tracked": KnownProject(
                key="tracked", path=tracked_path, name="Tracked",
                kind=ProjectKind.GIT, tracked=True,
            ),
            "ghost": KnownProject(
                key="ghost", path=ghost_path, name="Ghost",
                kind=ProjectKind.GIT, tracked=False,
            ),
        },
    })()

    ctx = RailContext(extras={"config": config})
    waiting = _user_waiting_task_ids(ctx)
    assert "tracked/1" in waiting
    assert "ghost/99" not in waiting


def test_classify_projects_review_status_does_not_set_has_working(
    tmp_path: Path,
) -> None:
    """A project whose only non-terminal task sits in ``review`` must
    NOT be flagged as having a working task — review = waiting on the
    user/reviewer to act, not an active agent turn.

    Regression for the booktalk stuck-spinner case (2026-04-25): the
    rail row for any project with a task in ``code_review`` /
    ``user_approval`` would render the green spinning glyph forever,
    even with the worker idle at its prompt and no live session in a
    turn. Mirror of ``cockpit_project_state._is_automated_progress``
    which already excludes ``review`` from its ``WORKING`` predicate.
    """
    import sqlite3

    from pollypm.plugins_builtin.core_rail_items.plugin import (
        _classify_projects,
    )

    def _seed(db_path: Path, project: str, number: int, status: str) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE work_tasks ("
                "project TEXT, task_number INTEGER, "
                "work_status TEXT, updated_at TEXT)"
            )
            conn.execute(
                "INSERT INTO work_tasks VALUES (?, ?, ?, ?)",
                (project, number, status, "2026-04-25T12:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    review_path = tmp_path / "review_only"
    _seed(review_path / ".pollypm" / "state.db", "review_only", 1, "review")

    inflight_path = tmp_path / "inflight"
    _seed(inflight_path / ".pollypm" / "state.db", "inflight", 1, "in_progress")

    config = type("C", (), {
        "projects": {
            "review_only": KnownProject(
                key="review_only", path=review_path, name="ReviewOnly",
                kind=ProjectKind.GIT, tracked=True,
            ),
            "inflight": KnownProject(
                key="inflight", path=inflight_path, name="InFlight",
                kind=ProjectKind.GIT, tracked=True,
            ),
        },
    })()

    class _StubRouter:
        _project_activity_cache: dict = {}

    ctx = RailContext(extras={"config": config, "router": _StubRouter()})
    _active, _inactive, has_working = _classify_projects(ctx)

    # The review-only project must NOT trigger the working spinner.
    assert has_working["review_only"] is False, (
        "review status should not flag has_working — "
        "the rail spinner spins on '◆ working' state, "
        "and review = waiting on user, not an active turn"
    )
    # Sanity: a real in_progress task DOES flag the spinner.
    assert has_working["inflight"] is True


def test_active_task_numbers_excludes_terminal_and_review_tasks(
    tmp_path: Path,
) -> None:
    """#1002 — the rail's per-project ``Task #N`` rows are sourced from
    ``work_tasks`` (DB truth), not from a tmux walk of the storage
    closet. The previous implementation enumerated ``task-<project>-<N>``
    windows and surfaced any window the tmux server happened to know
    about — including zombie windows left behind after the planning
    pipeline completed. Reading from the DB instead means a missed
    cleanup never leaks into the rail.

    This test seeds tasks across every status and pins the contract:
    only ``in_progress`` and ``rework`` tasks (i.e. tasks that have an
    active claim and are doing work) surface as worker rows.
    """
    import sqlite3
    from pollypm.plugins_builtin.core_rail_items.plugin import active_task_numbers
    from pollypm.models import KnownProject, ProjectKind

    project_root = tmp_path / "demo"
    (project_root / ".pollypm").mkdir(parents=True)
    db_path = project_root / ".pollypm" / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE work_tasks ("
            "  project TEXT NOT NULL,"
            "  task_number INTEGER NOT NULL,"
            "  work_status TEXT NOT NULL,"
            "  PRIMARY KEY (project, task_number)"
            ")"
        )
        # Cover every status the rail might see post-planning. The
        # bikepath-shaped input: critic tasks done, plan_project review,
        # one implementation task in_progress, one rework, the rest
        # blocked / cancelled.
        for number, status in [
            (1, "draft"),
            (2, "queued"),
            (3, "in_progress"),     # surface
            (4, "rework"),           # surface
            (5, "blocked"),
            (6, "on_hold"),
            (7, "review"),           # do NOT surface
            (8, "done"),             # do NOT surface
            (9, "cancelled"),        # do NOT surface
        ]:
            conn.execute(
                "INSERT INTO work_tasks(project, task_number, work_status) "
                "VALUES (?, ?, ?)",
                ("demo", number, status),
            )
        conn.commit()
    finally:
        conn.close()

    project = KnownProject(
        key="demo", path=project_root, name="Demo",
        kind=ProjectKind.GIT, tracked=True,
    )

    assert active_task_numbers(project) == [3, 4]


def test_active_task_numbers_handles_missing_db_gracefully(
    tmp_path: Path,
) -> None:
    """A project without a ``state.db`` (e.g. fresh / never-touched)
    must not raise; the rail just shows no per-task rows."""
    from pollypm.plugins_builtin.core_rail_items.plugin import active_task_numbers
    from pollypm.models import KnownProject, ProjectKind

    project = KnownProject(
        key="empty", path=tmp_path / "empty", name="Empty",
        kind=ProjectKind.GIT, tracked=True,
    )
    assert active_task_numbers(project) == []
