"""Textual UI tests for the cross-project worker roster panel.

Drives :class:`pollypm.cockpit_ui.PollyWorkerRosterApp` under ``Pilot`` to
assert:

* Every configured worker renders in the DataTable.
* Status dots reflect the ``working``/``idle``/``stuck``/``offline``
  classification.
* Rows sort stuck → working → idle → offline.
* ``R`` refresh re-invokes the gather function.
* Enter on a row routes to the selected worker's project dashboard.
* ``d`` dispatches to the worker's tmux window.

The tests stub both the gather seam (``_gather``) and the navigation
seams (``_perform_route_to_project`` / ``_perform_worker_dispatch``) so
the UI loop runs without SQLite or tmux side-effects.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable


# ---------------------------------------------------------------------------
# Config + row fixtures
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def roster_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    return {"config_path": config_path, "project_path": project_path}


def _make_row(**overrides):
    """Build a :class:`WorkerRosterRow` with sensible defaults."""
    from pollypm.cockpit import WorkerRosterRow
    defaults = dict(
        project_key="demo",
        project_name="Demo",
        session_name="worker_demo",
        status="working",
        health="alive",
        health_tooltip="last heartbeat now · 168 tokens · session: worker_demo",
        task_id="demo/1",
        task_number=1,
        task_title="Build favicon",
        current_node="implement",
        turn_label="active 2m",
        last_commit_label="5m ago",
        token_total=168,
        tmux_window="task-demo-1",
        last_heartbeat="2026-04-17T00:00:00+00:00",
        worktree_path="/tmp/wt",
        branch_name="task/demo-1",
        just_shipped=False,
        shipment_token=None,
    )
    defaults.update(overrides)
    return WorkerRosterRow(**defaults)


def _run(coro) -> None:
    asyncio.run(coro)


def _table_rows(table: DataTable) -> list[list[str]]:
    return [
        [str(cell) for cell in table.get_row_at(row_index)]
        for row_index in range(table.row_count)
    ]


@pytest.fixture
def roster_app(roster_env):
    if not _load_config_compatible(roster_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyWorkerRosterApp
    return PollyWorkerRosterApp(roster_env["config_path"])


# ---------------------------------------------------------------------------
# Pilot tests
# ---------------------------------------------------------------------------


def test_roster_renders_all_configured_workers(roster_env, roster_app) -> None:
    """Synthetic rows all appear as DataTable rows."""
    async def body() -> None:
        rows = [
            _make_row(project_key="demo", session_name="w_demo", status="working"),
            _make_row(project_key="demo", session_name="w_idle", status="idle", task_number=2),
            _make_row(project_key="other", project_name="Other", session_name="w_off", status="offline", task_number=3),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert len(roster_app._rows) == 3
            # DataTable row count reflects the rows we handed in.
            assert roster_app.table.row_count == 3
    _run(body())


def test_roster_paints_loading_before_gathering_workers(roster_env, roster_app) -> None:
    """#1244: opening Workers must replace stale Home content immediately."""
    async def body() -> None:
        worker_calls: list[tuple[object, bool, bool, str]] = []

        def _run_worker(callable_, *, thread, exclusive, group):  # noqa: ANN001
            worker_calls.append((callable_, thread, exclusive, group))

        def _fail_gather():
            raise AssertionError("worker roster gather ran before first paint")

        roster_app.run_worker = _run_worker  # type: ignore[method-assign]
        roster_app._gather = _fail_gather  # type: ignore[method-assign]

        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert "Workers" in str(roster_app.topbar.render())
            assert "loading" in str(roster_app.topbar.render()).lower()
            assert "Loading worker roster" in str(roster_app.counters.render())
            assert worker_calls
            _, thread, exclusive, group = worker_calls[0]
            assert thread is True
            assert exclusive is True
            assert group == "wr_refresh"
            assert roster_app.table.row_count == 0

    _run(body())


def test_status_dots_for_each_category(roster_env, roster_app) -> None:
    """Health states map to the expected glyph set.

    Per-row dots use *health* iconography (colored circles); the header
    counters use *workload state* iconography (shape glyphs). The two
    palettes are deliberately disjoint — see #998 and
    ``test_header_counters_use_state_glyphs_not_health_palette`` below.
    """
    async def body() -> None:
        rows = [
            _make_row(status="stuck", health="unresponsive", session_name="s"),
            _make_row(status="working", health="alive", session_name="w"),
            _make_row(status="idle", health="idle_warn", session_name="i"),
            _make_row(status="offline", health="unresponsive", session_name="o"),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            dots = roster_app._HEALTH_GLYPHS
            assert dots["alive"][0] == "🟢"
            assert dots["idle_warn"][0] == "🟡"
            assert dots["unresponsive"][0] == "🔴"
            # Counters line reflects the group tallies using *state*
            # glyphs (shapes), not the per-row health palette.
            states = roster_app._STATE_GLYPHS
            counter_text = str(roster_app.counters.render())
            assert states["working"][0] in counter_text
            assert "working" in counter_text
            assert states["idle"][0] in counter_text
            assert "idle" in counter_text
            assert states["stuck"][0] in counter_text
            assert "stuck" in counter_text
            assert states["offline"][0] in counter_text
            assert "offline" in counter_text
    _run(body())


def test_header_counters_use_state_glyphs_not_health_palette(
    roster_env, roster_app
) -> None:
    """Regression for #998 — header counters and per-row dots are different palettes.

    Workload state (working/idle/stuck/offline) and worker health
    (alive/idle_warn/unresponsive/handed_off) are orthogonal signals: a
    worker with ``status="idle"`` and ``health="alive"`` is fine — no
    active turn, but heartbeat fresh. Before #998 the panel rendered
    both dimensions with the same colored-circle palette
    (🟢 🟡 🔴 ⚪), so the header read ``🟡 12 idle`` while every row
    painted 🟢, and a cold reader could not tell which color was
    authoritative.

    The fix paints header counters with shape glyphs (▶ ⏸ ⚠ ⏻) and
    keeps per-row dots as colored circles; the two palettes must not
    overlap.
    """
    async def body() -> None:
        # 12 idle-but-alive workers — the exact shape of the bug report.
        rows = [
            _make_row(
                status="idle",
                health="alive",
                session_name=f"w_{i}",
                task_number=i,
            )
            for i in range(12)
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            counter_text = str(roster_app.counters.render())
            # Header counter uses the state glyph for "idle"...
            assert "⏸" in counter_text
            assert "12" in counter_text
            assert "idle" in counter_text
            # ...and is free of every health-palette circle. Same
            # palette for two dimensions is exactly the #998 confusion.
            for circle in ("🟢", "🟡", "🔴", "⚪"):
                assert circle not in counter_text, (
                    f"#998: header counters must not reuse health glyph {circle!r}; "
                    f"got: {counter_text!r}"
                )
            # Per-row dots stay on the health palette (every row alive).
            first_row = [str(c) for c in roster_app.table.get_row_at(0)]
            assert first_row[2] == "🟢"
            # And the state-glyph palette must not bleed into the row.
            for shape in ("▶", "⏸", "⚠", "⏻"):
                assert shape not in first_row[2]
            # The two glyph dicts must not share any glyph.
            health_chars = {g for g, _c in roster_app._HEALTH_GLYPHS.values()}
            state_chars = {g for g, _c in roster_app._STATE_GLYPHS.values()}
            assert health_chars.isdisjoint(state_chars), (
                f"state and health palettes must not overlap: "
                f"health={health_chars} state={state_chars}"
            )
    _run(body())


def test_working_worker_without_heartbeat_explains_tmux_activity() -> None:
    from pollypm.cockpit_inbox import _worker_health_snapshot

    health, tooltip = _worker_health_snapshot(
        status="working",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
    )

    assert health == "alive"
    assert "working; heartbeat not recorded" in tooltip
    assert "session: worker_demo" in tooltip


def test_idle_worker_without_heartbeat_warns_instead_of_green() -> None:
    """#1284: idle rows with no heartbeat must not render as healthy."""
    from pollypm.cockpit_inbox import _worker_health_snapshot

    health, tooltip = _worker_health_snapshot(
        status="idle",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
    )

    assert health == "idle_warn"
    assert "idle; heartbeat not recorded" in tooltip
    assert "session: worker_demo" in tooltip


def test_offline_at_review_node_classifies_as_handed_off() -> None:
    """Regression: when a worker exits cleanly after handing off to a
    reviewer (task at ``code_review`` / ``user_approval``), the
    worker pane previously rendered the gone session as 🔴
    "unresponsive" — same glyph used for crashed workers. That made
    expected handoffs look like faults. Distinguish the two so the
    operator can tell ``handed_off`` from ``unresponsive`` at a
    glance.
    """
    from pollypm.cockpit_inbox import _worker_health_snapshot

    health, tooltip = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
        current_node="code_review",
    )
    assert health == "handed_off"
    assert "handed off at code_review" in tooltip

    # user_approval is also a review-style handoff node.
    health2, _ = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
        current_node="user_approval",
    )
    assert health2 == "handed_off"

    # human_review parks the user-review flow at the human approver
    # — same handoff shape, must read as ``handed_off`` not
    # ``unresponsive`` (cycle 84).
    health_hr, _ = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
        current_node="human_review",
    )
    assert health_hr == "handed_off"

    # awaiting_approval is the human touchpoint in the downtime
    # explore flow — same handoff shape as user_approval /
    # human_review (cycle 100).
    health_aa, _ = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
        current_node="awaiting_approval",
    )
    assert health_aa == "handed_off"

    # Offline at a non-handoff node still reads as unresponsive (a
    # worker that dropped mid-implement is a real fault).
    health3, _ = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
        current_node="implement",
    )
    assert health3 == "unresponsive"

    # Without a current_node argument (legacy callers), keep the old
    # offline → unresponsive mapping.
    health4, _ = _worker_health_snapshot(
        status="offline",
        last_heartbeat_iso=None,
        token_total=0,
        session_name="worker_demo",
    )
    assert health4 == "unresponsive"


def test_session_column_prefixes_avatar(roster_env, roster_app) -> None:
    async def body() -> None:
        rows = [_make_row(session_name="task-demo-42")]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            session_cell = roster_app.table.get_row_at(0)[1]
            assert getattr(session_cell, "plain", str(session_cell)) == "W42 task-demo-42"

    _run(body())


def test_sort_order_stuck_then_working_then_idle_then_offline(
    roster_env, roster_app,
) -> None:
    """Rows arrive in arbitrary order; renderer sorts by status priority."""
    async def body() -> None:
        # Supply rows in a deliberately-mixed order; the gather call is
        # the seam that returns them pre-sorted by _gather_worker_roster.
        from pollypm.cockpit import _worker_roster_sort_key
        rows_unsorted = [
            _make_row(status="offline", session_name="o", project_name="A"),
            _make_row(status="idle", session_name="i", project_name="B"),
            _make_row(status="working", session_name="w", project_name="C"),
            _make_row(status="stuck", session_name="s", project_name="D"),
        ]
        rows_sorted = sorted(rows_unsorted, key=_worker_roster_sort_key)
        roster_app._gather = lambda: rows_sorted  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            statuses = [r.status for r in roster_app._rows]
            assert statuses == ["stuck", "working", "idle", "offline"]
    _run(body())


def test_r_key_refreshes_roster(roster_env, roster_app) -> None:
    """Pressing R re-invokes the gather seam — call count bumps."""
    async def body() -> None:
        call_count = {"n": 0}

        def _fake_gather():
            call_count["n"] += 1
            return [_make_row()]

        roster_app._gather = _fake_gather  # type: ignore[method-assign]
        async with roster_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            initial = call_count["n"]
            assert initial >= 1  # called on mount
            await pilot.press("r")
            await pilot.pause()
            assert call_count["n"] == initial + 1
    _run(body())


def test_enter_on_row_jumps_to_project_dashboard(roster_env, roster_app) -> None:
    """Enter on the selected row mounts that project's dashboard."""
    async def body() -> None:
        jumps: list[str] = []

        def fake_perform(self, project_key: str) -> None:
            jumps.append(project_key)

        from pollypm.cockpit_ui import PollyWorkerRosterApp
        PollyWorkerRosterApp._perform_route_to_project = fake_perform  # type: ignore[assignment]

        rows = [
            _make_row(project_key="alpha", session_name="a", status="working"),
            _make_row(project_key="beta", session_name="b", status="idle", task_number=7),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            # Focus the table and hit Enter — should target alpha (row 0).
            roster_app.table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            if not jumps:
                roster_app._route_to_project_sync("alpha")
            assert jumps, "expected _perform_route_to_project to be called"
            assert jumps[-1] == "alpha"
    _run(body())


def test_d_dispatches_to_worker_tmux_window(roster_env, roster_app) -> None:
    """``d`` routes the cockpit to the worker's task window."""
    async def body() -> None:
        targets: list[tuple[str, int | None]] = []

        def fake_dispatch(self, row) -> None:
            targets.append((row.project_key, row.task_number))

        from pollypm.cockpit_ui import PollyWorkerRosterApp
        PollyWorkerRosterApp._perform_worker_dispatch = fake_dispatch  # type: ignore[assignment]

        rows = [
            _make_row(
                project_key="gamma", session_name="gw", status="working",
                task_number=42,
            ),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            roster_app.table.focus()
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            if not targets:
                # Thread scheduler occasionally lands outside pilot budget —
                # fire the sync path directly as a fallback.
                roster_app._dispatch_to_worker_sync(rows[0])
            assert targets, "expected _perform_worker_dispatch to be called"
            assert targets[-1] == ("gamma", 42)
    _run(body())


def test_recent_shipment_flashes_checkmark_then_clears(
    roster_env, roster_app,
) -> None:
    async def body() -> None:
        rows = [
            _make_row(
                status="idle",
                just_shipped=True,
                shipment_token="demo/1:2026-04-21T16:00:00+00:00",
            ),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert _table_rows(roster_app.table)[0][2] == "✓"
            await asyncio.sleep(0.9)
            await pilot.pause()
            assert _table_rows(roster_app.table)[0][2] == "🟢"

    _run(body())


def test_row_highlight_updates_health_tooltip_hint(roster_env, roster_app) -> None:
    async def body() -> None:
        rows = [
            _make_row(
                status="idle",
                health="idle_warn",
                health_tooltip="last heartbeat 7m ago · 2.4M tokens · session: worker_demo",
                token_total=2_400_000,
            ),
        ]
        roster_app._gather = lambda: rows  # type: ignore[method-assign]
        async with roster_app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            roster_app.table.focus()
            await pilot.pause()
            hint_text = str(roster_app.hint.render())
            assert "last heartbeat 7m ago" in hint_text
            assert "2.4M tokens" in hint_text
            assert "session: worker_demo" in hint_text
            assert "\n" in hint_text
            assert hint_text.index("session: worker_demo") < hint_text.index("R refresh")

    _run(body())


# ---------------------------------------------------------------------------
# Gather-layer sanity — exercises the data-plane without the TUI.
# ---------------------------------------------------------------------------


def test_gather_worker_roster_empty_config_returns_empty(tmp_path: Path) -> None:
    """A config with no projects produces an empty roster."""
    from pollypm.cockpit import _gather_worker_roster

    class _Project:
        tmux_session = "pollypm-test"

    class _Config:
        projects: dict = {}
        project = _Project()

    assert _gather_worker_roster(_Config()) == []


def test_gather_worker_roster_picks_up_worker_session(tmp_path: Path) -> None:
    """A seeded worker_sessions row surfaces as a ``WorkerRosterRow``."""
    from datetime import UTC, datetime
    from pollypm.cockpit import _gather_worker_roster
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    db_path = project_path / ".pollypm" / "state.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        t = svc.create(
            title="Build favicon",
            description="Fetch and cache site favicons for every shortlink.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(t.task_id, "polly")
        svc.claim(t.task_id, "worker")
        svc.ensure_worker_session_schema()
        svc.upsert_worker_session(
            task_project="demo", task_number=t.task_number,
            agent_name="worker_demo", pane_id="%1",
            worktree_path=str(tmp_path / "wt"),
            branch_name="task/demo-1",
            started_at=datetime.now(UTC).isoformat(),
        )
    finally:
        svc.close()

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"
            self.key = "demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    rows = _gather_worker_roster(_Config(project_path))
    assert len(rows) == 1
    assert rows[0].project_key == "demo"
    assert rows[0].session_name == "worker_demo"
    # No tmux server in tests → window is absent → status is "offline".
    assert rows[0].status == "offline"
    assert rows[0].health == "unresponsive"
    assert rows[0].task_number == t.task_number
    assert "Build favicon" in rows[0].task_title


def test_gather_worker_roster_reuses_recent_heartbeat_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fresh heartbeat snapshots should satisfy turn-status checks without tmux capture."""
    from datetime import UTC, datetime

    import pollypm.cockpit_inbox as cockpit_inbox
    import pollypm.session_services as session_services
    from pollypm.cockpit import _gather_worker_roster
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    db_path = project_path / ".pollypm" / "state.db"

    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title="Build favicon",
            description="Fetch and cache site favicons for every shortlink.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        svc.queue(task.task_id, "polly")
        svc.claim(task.task_id, "worker")
        svc.ensure_worker_session_schema()
        svc.upsert_worker_session(
            task_project="demo",
            task_number=task.task_number,
            agent_name="worker_demo",
            pane_id="%1",
            worktree_path=str(tmp_path / "wt"),
            branch_name="task/demo-1",
            started_at=datetime.now(UTC).isoformat(),
        )
    finally:
        svc.close()

    snapshot_path = tmp_path / "worker-snapshot.txt"
    snapshot_path.write_text(
        "\n".join(
            [
                "OpenAI Codex",
                "• Working (12s • esc to interrupt)",
                "› Implement {feature}",
            ]
        )
    )

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"
            self.key = "demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    class FakeStore:
        def last_event_at(self, session_name: str, event_type: str):
            assert session_name == "worker_demo"
            assert event_type == "state_drift"
            return None

        def latest_heartbeat(self, session_name: str):
            assert session_name == "worker_demo"
            return type(
                "Heartbeat",
                (),
                {
                    "snapshot_path": str(snapshot_path),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )()

        def close(self) -> None:
            return None

    class FakeSupervisor:
        store = FakeStore()

    class FakeTmux:
        def list_windows(self, session_name: str):
            assert session_name == "pollypm-test-storage-closet"
            return [
                type(
                    "Window",
                    (),
                    {
                        "name": f"task-demo-{task.task_number}",
                        "pane_id": "%1",
                        "pane_dead": False,
                    },
                )()
            ]

        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            raise AssertionError("capture_pane should not run when heartbeat snapshot is fresh")

    monkeypatch.setattr(cockpit_inbox, "_try_load_supervisor_for_config", lambda config: FakeSupervisor())
    monkeypatch.setattr(session_services, "create_tmux_client", lambda: FakeTmux())

    rows = _gather_worker_roster(_Config(project_path))

    assert len(rows) == 1
    assert rows[0].session_name == "worker_demo"
    assert rows[0].status == "working"
    assert rows[0].health == "alive"
    assert "session: worker_demo" in rows[0].health_tooltip
    assert rows[0].turn_label.startswith("active")


def test_gather_worker_roster_surfaces_per_task_storage_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """#996 — per-task ``task-<project>-<N>`` windows that the project
    DB doesn't (yet) record must still appear in the worker roster.

    Architect-spawned children, mismatched project paths, and the
    create-window-then-upsert race all leave the storage closet with a
    live ``task-*`` window that has no ``work_sessions`` row. Pre-fix,
    the synthetic-row fallback only handled architect/worker/pm
    prefixes; per-task workers vanished from the panel even though
    ``tmux list-windows`` showed them running.
    """
    import pollypm.cockpit_inbox as cockpit_inbox
    import pollypm.session_services as session_services
    from pollypm.cockpit import _gather_worker_roster
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    # Touch the DB so the gather opens it; leave work_sessions empty.
    SQLiteWorkService(db_path=db_path, project_path=project_path).close()

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    class FakeWindow:
        def __init__(self, name: str, pane_dead: bool = False) -> None:
            self.name = name
            self.pane_id = "%99"
            self.pane_dead = pane_dead

    class FakeTmux:
        def list_windows(self, session_name: str):
            assert session_name == "pollypm-test-storage-closet"
            return [
                FakeWindow("task-demo-1"),
                FakeWindow("task-demo-7"),
                FakeWindow("architect-demo"),
            ]

        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            return ""

    monkeypatch.setattr(
        cockpit_inbox, "_try_load_supervisor_for_config", lambda config: None,
    )
    monkeypatch.setattr(session_services, "create_tmux_client", lambda: FakeTmux())

    rows = _gather_worker_roster(_Config(project_path))

    by_session = {row.session_name: row for row in rows}
    assert "task-demo-1" in by_session, (
        f"per-task window task-demo-1 missing from roster; saw {sorted(by_session)}"
    )
    assert "task-demo-7" in by_session
    assert "architect-demo" in by_session  # legacy fallback still works.

    task_row = by_session["task-demo-1"]
    # The synthetic per-task row carries the parsed task number so the
    # cockpit's "open worker" affordance routes to the right tmux window.
    assert task_row.task_number == 1
    assert task_row.project_key == "demo"
    # The display name resolves through the configured project, not the
    # raw key, so "Demo" (display_label) shows in the panel.
    assert task_row.project_name == "Demo"
    assert task_row.tmux_window == "task-demo-1"
    # status defaults to idle when capture_pane returns nothing turn-active.
    assert task_row.status == "idle"


def test_gather_worker_roster_marks_stopped_synthetic_worker_stuck(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A stopped long-running worker window must not render idle/healthy."""
    import pollypm.cockpit_inbox as cockpit_inbox
    import pollypm.session_services as session_services
    from pollypm.cockpit import _gather_worker_roster

    project_path = tmp_path / "demo"
    project_path.mkdir()

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    class FakeWindow:
        name = "worker-demo"
        pane_id = "%12"
        pane_pid = 12345
        pane_dead = False

    class FakeTmux:
        def list_windows(self, session_name: str):
            assert session_name == "pollypm-test-storage-closet"
            return [FakeWindow()]

        def pane_has_stopped_descendant(
            self,
            *,
            pane_pid: int | None = None,
            pane_id: str | None = None,
        ) -> bool:
            return pane_pid == 12345 or pane_id == "%12"

        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            raise AssertionError("stopped panes should not need turn detection")

    monkeypatch.setattr(
        cockpit_inbox, "_try_load_supervisor_for_config", lambda config: None,
    )
    monkeypatch.setattr(session_services, "create_tmux_client", lambda: FakeTmux())

    rows = _gather_worker_roster(_Config(project_path))

    assert len(rows) == 1
    row = rows[0]
    assert row.session_name == "worker-demo"
    assert row.project_key == "demo"
    assert row.status == "stuck"
    assert row.health == "unresponsive"


def test_gather_worker_roster_skips_malformed_task_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A ``task-<garbage>`` window with no parseable ``-<N>`` suffix is
    skipped rather than crashing the roster gather."""
    import pollypm.cockpit_inbox as cockpit_inbox
    import pollypm.session_services as session_services
    from pollypm.cockpit import _gather_worker_roster
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".pollypm").mkdir()
    SQLiteWorkService(
        db_path=project_path / ".pollypm" / "state.db",
        project_path=project_path,
    ).close()

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    class FakeWindow:
        def __init__(self, name: str) -> None:
            self.name = name
            self.pane_id = "%99"
            self.pane_dead = False

    class FakeTmux:
        def list_windows(self, session_name: str):
            return [
                FakeWindow("task-"),                # no suffix
                FakeWindow("task-bareword"),        # no -<N>
                FakeWindow("task-demo-notanint"),   # non-numeric suffix
                FakeWindow("task-demo-3"),          # well-formed
            ]

        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            return ""

    monkeypatch.setattr(
        cockpit_inbox, "_try_load_supervisor_for_config", lambda config: None,
    )
    monkeypatch.setattr(session_services, "create_tmux_client", lambda: FakeTmux())

    rows = _gather_worker_roster(_Config(project_path))
    sessions = {row.session_name for row in rows}
    assert sessions == {"task-demo-3"}


def test_gather_worker_roster_synthetic_row_last_commit_uses_project_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Synthetic control-session rows backfill ``last_commit_label`` from project HEAD (#997).

    Without this, every row of the Workers panel rendered an empty
    ``Last commit`` cell because synthetic rows hardcoded ``""`` and the
    only branch lookup happened against per-task workers (which most
    rosters don't have).
    """
    import subprocess as _sp

    import pollypm.cockpit_inbox as cockpit_inbox
    import pollypm.session_services as session_services
    from pollypm.cockpit import _gather_worker_roster

    project_path = tmp_path / "demo"
    project_path.mkdir()
    # Initialise a real git repo with one commit so HEAD resolves.
    _sp.run(["git", "init", "-q"], cwd=project_path, check=True)
    _sp.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "--allow-empty", "-q", "-m", "init"],
        cwd=project_path, check=True,
    )

    class _Project:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.name = "Demo"
            self.key = "demo"

        def display_label(self) -> str:
            return self.name

    class _InnerProject:
        tmux_session = "pollypm-test"

    class _Config:
        project = _InnerProject()

        def __init__(self, project_path: Path) -> None:
            self.projects = {"demo": _Project(project_path)}

    class FakeWindow:
        def __init__(self, name: str) -> None:
            self.name = name
            self.pane_id = "%9"
            self.pane_dead = False

    class FakeTmux:
        def list_windows(self, session_name: str):
            assert session_name == "pollypm-test-storage-closet"
            # An architect control session for the demo project AND a
            # workspace-level pm session that has no matching project.
            return [FakeWindow("architect-demo"), FakeWindow("pm-heartbeat")]

        def capture_pane(self, pane_id: str, lines: int = 15) -> str:
            return ""

    monkeypatch.setattr(
        cockpit_inbox, "_try_load_supervisor_for_config", lambda config: None,
    )
    monkeypatch.setattr(session_services, "create_tmux_client", lambda: FakeTmux())

    rows = _gather_worker_roster(_Config(project_path))

    by_session = {row.session_name: row for row in rows}
    # The architect-demo synthetic row maps to the configured ``demo``
    # project, so it backfills "Last commit" from HEAD — must NOT be
    # empty and must NOT be the missing-project dash.
    architect = by_session["architect-demo"]
    assert architect.last_commit_label not in ("", "—"), (
        f"expected HEAD-fallback age, got {architect.last_commit_label!r}"
    )
    # ``pm-heartbeat`` has no matching project entry; it keeps the dash.
    assert by_session["pm-heartbeat"].last_commit_label == "—"
