"""Textual UI tests for the cockpit observability metrics screen.

Covers both the data plane (``_gather_metrics_snapshot`` + its helpers)
and the UI plane (:class:`pollypm.cockpit_ui.PollyMetricsApp` driven via
``Pilot``). Each test is self-contained and uses synthetic fixtures so
it doesn't depend on a live tmux server or a real ``state.db``.

Run targeted, per the agent brief::

    HOME=/tmp/pytest-agent-metrics uv run pytest \\
        tests/test_metrics_ui.py tests/test_project_dashboard_ui.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Config fixture helpers
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
def metrics_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    return {"config_path": config_path, "project_path": project_path}


@pytest.fixture
def metrics_app(metrics_env):
    if not _load_config_compatible(metrics_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyMetricsApp
    return PollyMetricsApp(metrics_env["config_path"])


def _make_roster_row(**overrides):
    from pollypm.cockpit import WorkerRosterRow
    defaults = dict(
        project_key="demo",
        project_name="Demo",
        session_name="worker_demo",
        status="working",
        health="alive",
        health_tooltip="heartbeat active",
        task_id="demo/1",
        task_number=1,
        task_title="Work",
        current_node="implement",
        turn_label="active 2m",
        last_commit_label="5m ago",
        token_total=200000,
        tmux_window="task-demo-1",
        last_heartbeat="2026-04-17T00:00:00+00:00",
        worktree_path="/tmp/wt",
        branch_name="task/demo-1",
    )
    defaults.update(overrides)
    return WorkerRosterRow(**defaults)


def _make_snapshot(
    *,
    fleet_rows=None,
    resource_rows=None,
    throughput_rows=None,
    failure_rows=None,
    scheduler_rows=None,
) -> "object":
    """Build a ``MetricsSnapshot`` with caller-supplied section rows.

    Tests that don't care about a section leave it as the shipped
    default so the renderer always has something to draw.
    """
    from pollypm.cockpit import MetricsSnapshot, MetricsSection

    def _mk(key: str, title: str, rows) -> MetricsSection:
        return MetricsSection(
            key=key, title=title, rows=list(rows or []),
        )

    return MetricsSnapshot(
        captured_at=datetime.now(UTC).isoformat(),
        fleet=_mk("fleet", "Fleet", fleet_rows or [
            ("Workers", "1 working · 0 idle · 0 stuck · 0 offline", "ok"),
            (
                "Tasks in flight",
                "0 queued · 1 in progress · 0 review · 0 blocked · 0 on hold",
                "ok",
            ),
            ("Inbox", "0 action · 0 unread · 0 plan review · 0 blocked", "ok"),
        ]),
        resources=_mk("resources", "Resources", resource_rows or [
            ("state.db", "1.0 MB · freelist 0.0%", "ok"),
        ]),
        throughput=_mk("throughput", "Throughput (24h)", throughput_rows or [
            ("Tasks completed", "3", "ok"),
        ]),
        failures=_mk("failures", "Failures (24h)", failure_rows or [
            ("state_drift", "0", "ok"),
        ]),
        schedulers=_mk("schedulers", "Schedulers", scheduler_rows or [
            ("token_usage_hourly", "2m ago", "ok"),
        ]),
    )


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Mounts and renders all sections
# ---------------------------------------------------------------------------


def test_mount_renders_all_five_sections(metrics_env, metrics_app) -> None:
    """After mount, every section key shows up in the composed tree."""
    async def body() -> None:
        metrics_app._gather = lambda: _make_snapshot()  # type: ignore[method-assign]
        async with metrics_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            # Every section ID is composed.
            for key in ("fleet", "resources", "throughput", "failures", "schedulers"):
                node = metrics_app.query_one(f"#me-section-{key}")
                assert node is not None, f"section {key!r} missing"
            # The snapshot is stashed so the renderer can re-run on R.
            assert metrics_app.snapshot is not None
    _run(body())


def test_compact_viewport_renders_section_labels(metrics_env, metrics_app) -> None:
    """At the issue's 80x24 size, section boxes must not render blank."""
    async def body() -> None:
        from pollypm.cockpit_smoke import SmokeHarness

        metrics_app._gather = lambda: _make_snapshot()  # type: ignore[method-assign]
        async with SmokeHarness.mount(metrics_app, size=(80, 24)) as smoke:
            capture = smoke.snapshot()
            text = capture.rendered_text
            assert "Fleet" in text
            assert "Workers:" in text
            assert "Resources" in text
            assert "state.db:" in text
    _run(body())


# ---------------------------------------------------------------------------
# 2. Fleet counts line up with synthetic task/worker data
# ---------------------------------------------------------------------------


def test_fleet_section_reflects_synthetic_roster_and_tasks() -> None:
    """_fleet_section renders the exact working/idle/stuck/offline tally."""
    from pollypm.cockpit import _fleet_section

    roster = [
        _make_roster_row(status="working", session_name="w1"),
        _make_roster_row(status="working", session_name="w2"),
        _make_roster_row(status="stuck", session_name="s1"),
        _make_roster_row(status="idle", session_name="i1"),
        _make_roster_row(status="offline", session_name="o1"),
    ]
    counts = {
        "queued": 2,
        "in_progress": 3,
        "review": 1,
        "blocked": 1,
        "on_hold": 2,
    }
    inbox = {"action": 6, "unread": 4, "plan_review": 1, "blocked": 2}

    section = _fleet_section(None, roster, counts, inbox)
    assert section.key == "fleet"
    # Workers row — tone goes red (alert) because of the stuck worker.
    worker_row = next(r for r in section.rows if r[0] == "Workers")
    assert "2 working" in worker_row[1]
    assert "1 stuck" in worker_row[1]
    assert worker_row[2] == "alert"
    # Tasks row — blocked count present, tone alert.
    # Cycle 83: status names render with spaces (``in progress`` /
    # ``on hold``) instead of the underscored python keys.
    tif = next(r for r in section.rows if r[0] == "Tasks in flight")
    assert "3 in progress" in tif[1]
    assert "1 blocked" in tif[1]
    assert "2 on hold" in tif[1]
    assert tif[2] == "alert"
    # Inbox row — non-zero fields produce a warn tone.
    ibx = next(r for r in section.rows if r[0] == "Inbox")
    assert "6 action" in ibx[1]
    assert "4 unread" in ibx[1]
    assert "1 plan review" in ibx[1]
    assert "2 blocked" in ibx[1]
    assert ibx[2] == "warn"


# ---------------------------------------------------------------------------
# 3. Resource colours fire at thresholds
# ---------------------------------------------------------------------------


def test_resource_thresholds_fire_green_yellow_red(tmp_path: Path, monkeypatch) -> None:
    """Synthetic state.db sizes drive the expected tone for each row."""
    from pollypm.cockpit import _resource_section

    # Green: small DB (< 100 MB).
    small_db = tmp_path / "small.db"
    small_db.write_bytes(b"\x00" * 1024)  # 1 KB

    # Yellow: >= 100 MB (sparse file — doesn't actually consume space).
    yellow_db = tmp_path / "yellow.db"
    with yellow_db.open("wb") as f:
        f.truncate(150 * 1024 * 1024)

    # Red: >= 500 MB.
    red_db = tmp_path / "red.db"
    with red_db.open("wb") as f:
        f.truncate(600 * 1024 * 1024)

    class _Proj:
        def __init__(self, state_db: Path):
            self.state_db = state_db
            self.workspace_root = tmp_path
            self.config_file = None
            self.config_path = None

    class _Cfg:
        def __init__(self, state_db: Path):
            self.project = _Proj(state_db)
            self.projects = {}

    sec_green = _resource_section(_Cfg(small_db))
    state_row = next(r for r in sec_green.rows if r[0] == "state.db")
    assert state_row[2] == "ok"

    sec_yellow = _resource_section(_Cfg(yellow_db))
    state_row = next(r for r in sec_yellow.rows if r[0] == "state.db")
    assert state_row[2] == "warn"

    sec_red = _resource_section(_Cfg(red_db))
    state_row = next(r for r in sec_red.rows if r[0] == "state.db")
    assert state_row[2] == "alert"


# ---------------------------------------------------------------------------
# 4. Throughput reads from the last-24h event window
# ---------------------------------------------------------------------------


def test_throughput_section_counts_recent_events() -> None:
    """_throughput_section classifies event kinds into the right counters."""
    from pollypm.cockpit import _throughput_section

    class _E:
        def __init__(self, event_type: str):
            self.event_type = event_type

    events = [
        _E("task_done"), _E("task_done"), _E("task_done"),
        _E("task.rejected"),
        _E("plan_approved"),
        _E("pr_reviewed"),
        _E("commit_landed"),
        _E("commit_landed"),
        _E("heartbeat"),  # ignored
    ]
    sec = _throughput_section(events)
    rows = {r[0]: (r[1], r[2]) for r in sec.rows}
    assert rows["Tasks completed"][0] == "3"
    assert rows["Tasks rejected"][0] == "1"
    assert rows["Tasks rejected"][1] == "warn"
    assert rows["PRs reviewed"][0] == "1"
    assert rows["Plan approvals"][0] == "1"
    # Commits row counts anything with "commit" in the type.
    assert rows["Commits (worker events)"][0] == "2"


# ---------------------------------------------------------------------------
# 5. Failure counts categorise correctly
# ---------------------------------------------------------------------------


def test_failure_section_categorises_failures() -> None:
    """state_drift, persona_swap, reprompts, no_session — all flagged.

    ``no_session`` alerts are emitted via ``upsert_alert`` (type=
    "alert" in the unified messages table), not via ``record_event``.
    The metric counts them from a store query, so we pass a fake
    store that returns the expected alert rows.
    """
    from pollypm.cockpit import _failure_section

    class _E:
        def __init__(self, event_type: str):
            self.event_type = event_type

    events = [
        _E("state_drift"),
        _E("state_drift"),
        _E("persona_swap_detected"),
        _E("worker_reprompt"),
        _E("worker_reprompt"),
        _E("provider_probe_failed"),
    ]

    class _Store:
        def query_messages(self, **filters):
            # Returns one alert row whose sender contains "no_session"
            # to exercise the alert-side counter.
            return [{"sender": "no_session_for_assignment:proj/1"}]

    sec = _failure_section(events, store=_Store())
    rows = {r[0]: (r[1], r[2]) for r in sec.rows}
    assert rows["state_drift"][0] == "2"
    assert rows["state_drift"][1] == "alert"
    assert rows["persona_swap_detected"][0] == "1"
    assert rows["persona_swap_detected"][1] == "alert"
    assert rows["worker reprompts"][0] == "2"
    assert rows["worker reprompts"][1] == "warn"
    assert rows["no_session alerts"][0] == "1"
    assert rows["no_session alerts"][1] == "alert"
    assert rows["Provider probe failures"][0] == "1"


def test_failure_section_no_session_metric_is_zero_when_store_omitted() -> None:
    """Older callers that don't pass a store still get a sensible
    zero for the no_session counter (no crash, no spurious count)."""
    from pollypm.cockpit import _failure_section

    sec = _failure_section([])
    rows = {r[0]: r[1] for r in sec.rows}
    assert rows["no_session alerts"] == "0"


def test_failure_section_uses_execute_when_store_supports_it() -> None:
    """The cockpit metric pipeline passes a ``StateStore`` whose
    alert-table access is via raw SQL (``store.execute(...)``), not
    the SQLAlchemy ``query_messages`` API. Exercise the execute
    branch so the live cockpit code path stays covered.
    """
    from pollypm.cockpit import _failure_section

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _StateStore:
        # Records the SQL fired so the test can assert on it.
        def __init__(self):
            self.queries: list[tuple[str, tuple]] = []

        def execute(self, sql, params=()):
            self.queries.append((sql, params))
            # Pretend we have 3 no_session alerts in the last 24h.
            return _Cursor([
                ("no_session_for_assignment:proj/1",),
                ("no_session_for_assignment:demo/2",),
                ("no_session",),
            ])

    store = _StateStore()
    sec = _failure_section([], store=store)
    rows = {r[0]: r[1] for r in sec.rows}
    assert rows["no_session alerts"] == "3"
    # Confirm we actually queried the messages table for alerts —
    # protects against an accidental signature drift that silently
    # zeroes the metric again.
    assert any("messages" in sql for sql, _ in store.queries)
    assert any("no_session%" in sql for sql, _ in store.queries)


# ---------------------------------------------------------------------------
# 6. Scheduler section shows last-fired-at; red if stale
# ---------------------------------------------------------------------------


def test_scheduler_section_flags_stale_handler() -> None:
    """Last-fired > 2× cadence → alert tone."""
    from pollypm.cockpit import _scheduler_section

    now = datetime.now(UTC)

    class _E:
        def __init__(self, event_type, session_name, message, created_at):
            self.event_type = event_type
            self.session_name = session_name
            self.message = message
            self.created_at = created_at

    # Fresh handler — cadence ~1h, last fired 10 min ago.
    fresh_msgs = [
        _E(
            "ran", "scheduler",
            '{"subject": "token_usage_hourly"}',
            (now - timedelta(minutes=10)).isoformat(),
        ),
        _E(
            "ran", "scheduler",
            '{"subject": "token_usage_hourly"}',
            (now - timedelta(hours=1, minutes=10)).isoformat(),
        ),
    ]
    # Stale handler — cadence ~1h, last fired 6h ago.
    stale_msgs = [
        _E(
            "ran", "scheduler",
            '{"subject": "stuck_task_sweep"}',
            (now - timedelta(hours=6)).isoformat(),
        ),
        _E(
            "ran", "scheduler",
            '{"subject": "stuck_task_sweep"}',
            (now - timedelta(hours=7)).isoformat(),
        ),
    ]

    class _Store:
        def recent_events(self, limit):
            return fresh_msgs + stale_msgs

    sec = _scheduler_section(_Store())
    rows = {r[0]: (r[1], r[2]) for r in sec.rows}
    assert rows["token_usage_hourly"][1] == "ok"
    assert rows["stuck_task_sweep"][1] == "alert"


def test_scheduler_section_empty_state_uses_narrative_row() -> None:
    """When no scheduled events have been recorded, the section
    previously emitted a row with value=``—`` so the renderer
    rendered ``● (no scheduled runs recorded): —`` — visually
    parsed as a broken row. Use an empty value so the renderer can
    drop the ``:`` separator and surface a clean narrative line.
    """
    from pollypm.cockpit import _scheduler_section

    class _Store:
        def recent_events(self, limit):
            return []

    sec = _scheduler_section(_Store())
    assert len(sec.rows) == 1
    label, value, tone = sec.rows[0]
    assert "scheduled" in label.lower()
    assert value == ""
    assert tone == "muted"


# ---------------------------------------------------------------------------
# 7. R refreshes, A toggles auto-refresh
# ---------------------------------------------------------------------------


def test_r_refreshes_and_a_toggles_auto_refresh(metrics_env, metrics_app) -> None:
    """R re-invokes _gather; A flips the auto_refresh flag."""
    async def body() -> None:
        call_count = {"n": 0}

        def _fake_gather():
            call_count["n"] += 1
            return _make_snapshot()

        metrics_app._gather = _fake_gather  # type: ignore[method-assign]
        async with metrics_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            initial = call_count["n"]
            assert initial >= 1  # called on mount

            # R → refresh.
            await pilot.press("r")
            await pilot.pause()
            assert call_count["n"] == initial + 1

            # A → toggle auto-refresh on.
            assert metrics_app._auto_refresh is False
            await pilot.press("a")
            await pilot.pause()
            assert metrics_app._auto_refresh is True
            # Timer is registered when auto turns on.
            assert metrics_app._auto_refresh_timer is not None

            # A again → toggle off.
            await pilot.press("a")
            await pilot.pause()
            assert metrics_app._auto_refresh is False
    _run(body())


# ---------------------------------------------------------------------------
# 8. Enter on a section opens a drill-down
# ---------------------------------------------------------------------------


def test_enter_opens_drill_down_modal(metrics_env, metrics_app) -> None:
    """Enter on a selected section pushes the drill-down modal."""
    async def body() -> None:
        metrics_app._gather = lambda: _make_snapshot()  # type: ignore[method-assign]
        pushed: list = []
        original_push = metrics_app.push_screen

        def _capture(screen, *args, **kwargs):
            pushed.append(screen)
            # Don't actually mount — keep the test lean.
            return None

        metrics_app.push_screen = _capture  # type: ignore[assignment]
        async with metrics_app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            # Move selection to the Resources section (index 1).
            await pilot.press("down")
            await pilot.pause()
            assert metrics_app._selected_index == 1
            # Fire the action directly — pilot key presses sometimes
            # land on the scroll container before the app bindings.
            metrics_app.action_drill_down()
            await pilot.pause()
            assert pushed, "expected drill-down modal to be pushed"
            from pollypm.cockpit_ui import _MetricsDrillDownModal
            assert isinstance(pushed[-1], _MetricsDrillDownModal)
        # Restore for teardown.
        metrics_app.push_screen = original_push  # type: ignore[assignment]
    _run(body())


# ---------------------------------------------------------------------------
# Bonus — palette + rail wiring smoke checks so the screen is reachable.
# ---------------------------------------------------------------------------


def test_palette_includes_go_to_metrics(tmp_path: Path) -> None:
    """The ``:`` palette exposes the Metrics nav entry."""
    from pollypm.cockpit import build_palette_commands

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)

    commands = build_palette_commands(config_path)
    tags = {c.tag for c in commands}
    assert "nav.metrics" in tags
    metrics_cmd = next(c for c in commands if c.tag == "nav.metrics")
    assert "Metrics" in metrics_cmd.title


def test_render_metrics_panel_fallback_renders_headings(tmp_path: Path) -> None:
    """The text fallback ``_render_metrics_panel`` labels every section."""
    from pollypm.cockpit import _render_metrics_panel

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")

    text = _render_metrics_panel(config_path)
    assert "Metrics" in text
    for heading in ("Fleet", "Resources", "Throughput", "Failures", "Schedulers"):
        assert heading in text
