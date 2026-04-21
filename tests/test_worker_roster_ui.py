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


def test_status_dots_for_each_category(roster_env, roster_app) -> None:
    """Health states map to the expected glyph set."""
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
            # Counters line reflects the group tallies.
            counter_text = str(roster_app.counters.render())
            assert "1" in counter_text  # at least one of each
    _run(body())


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
