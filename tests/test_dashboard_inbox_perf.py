"""Focused perf-regression tests for dashboard inbox rollups."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace


def test_inbox_tasks_grouped_shares_scan_with_copy_safe_cache(
    monkeypatch,
) -> None:
    import pollypm.cockpit_pg_aggregates as agg

    agg._INBOX_TASKS_GROUPED_CACHE.clear()
    calls = {"n": 0}
    task = SimpleNamespace(task_id="demo/1", project="demo")

    def fake_uncached(_config):
        calls["n"] += 1
        return {"demo": [task]}

    monkeypatch.setattr(agg, "_inbox_tasks_grouped_uncached", fake_uncached)

    config = object()
    first = agg.inbox_tasks_grouped(config)
    assert first == {"demo": [task]}
    first["demo"].append(SimpleNamespace(task_id="demo/2", project="demo"))

    second = agg.inbox_tasks_grouped(config)

    assert calls["n"] == 1
    assert second == {"demo": [task]}


def test_grouped_task_caches_coalesce_concurrent_cold_misses(
    monkeypatch,
) -> None:
    import pollypm.cockpit_pg_aggregates as agg

    agg._ALL_TASKS_GROUPED_CACHE.clear()
    agg._INBOX_TASKS_GROUPED_CACHE.clear()
    calls = {"all": 0, "inbox": 0}
    task = SimpleNamespace(task_id="demo/1", project="demo")

    def fake_all(_config):
        calls["all"] += 1
        time.sleep(0.05)
        return {"demo": [task]}

    def fake_inbox(_config):
        calls["inbox"] += 1
        time.sleep(0.05)
        return {"demo": [task]}

    monkeypatch.setattr(agg, "_all_tasks_grouped_uncached", fake_all)
    monkeypatch.setattr(agg, "_inbox_tasks_grouped_uncached", fake_inbox)

    config = object()
    start = threading.Event()

    def call_both() -> tuple[dict, dict]:
        start.wait(timeout=1)
        return (
            agg.all_tasks_grouped(config) or {},
            agg.inbox_tasks_grouped(config) or {},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(call_both) for _ in range(8)]
        start.set()
        results = [future.result(timeout=2) for future in futures]

    assert all(result == ({"demo": [task]}, {"demo": [task]}) for result in results)
    assert calls == {"all": 1, "inbox": 1}


def test_completed_issues_coalesces_concurrent_cold_misses(
    tmp_path: Path, monkeypatch,
) -> None:
    """#2320 regression — N parallel cold callers share one fs walk.

    Without the lock + double-check around the miss path, 8 parallel
    threads all race past the empty-cache check and each runs an
    independent ``glob('*.md')`` against every project's
    ``05-completed/`` directory. The lock collapses them to one walk.
    """
    import pollypm.dashboard_data as dd

    dd._COMPLETED_ISSUES_CACHE.clear()

    # Build a fake config with two projects, each with a completed/ dir
    # populated with a single recent issue file so glob has something to
    # return and the cache writes a non-trivial result.
    projects = {}
    for alias in ("proj-a", "proj-b"):
        root = tmp_path / alias
        completed = root / "issues" / "05-completed"
        completed.mkdir(parents=True)
        (completed / "0001-demo.md").write_text("demo\n")
        projects[alias] = SimpleNamespace(path=root)

    config = SimpleNamespace(projects=projects)

    # Wrap Path.glob so we can count fs walks AND inject latency on the
    # cold path. Without the lock all 8 threads race past the empty
    # cache check before any of them writes, so we'd see 8 *
    # n_projects calls; with the lock we see exactly n_projects.
    real_glob = Path.glob
    glob_calls = {"n": 0}
    glob_lock = threading.Lock()

    def slow_glob(self, pattern, *args, **kwargs):
        with glob_lock:
            glob_calls["n"] += 1
        # Simulate filesystem walk latency so concurrent threads pile
        # up on the miss path. Without the singleflight lock they all
        # call into here.
        time.sleep(0.05)
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", slow_glob)

    start = threading.Event()

    def call() -> list:
        start.wait(timeout=1)
        return dd._completed_issues(config, hours=72)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(call) for _ in range(8)]
        start.set()
        results = [future.result(timeout=5) for future in futures]

    # All callers see the same result.
    assert all(len(r) == 2 for r in results)
    # Exactly one walk per project, not 8 * n_projects.
    assert glob_calls["n"] == len(projects), (
        f"expected {len(projects)} glob calls (one per project, "
        f"shared across 8 threads), got {glob_calls['n']}"
    )


def test_awaits_user_list_annotates_only_actionable_rows(
    tmp_path: Path, monkeypatch,
) -> None:
    from pollypm import cockpit_inbox
    from pollypm.inbox.kind import InboxItemKind

    actionable = SimpleNamespace(
        task_id="demo/1",
        project="demo",
        kind=InboxItemKind.APPROVAL_REQUEST,
        labels=[],
        title="Approve deployment",
        description="",
    )
    informational = SimpleNamespace(
        task_id="demo/2",
        project="demo",
        kind=InboxItemKind.COMPLETION_FYI,
        labels=[],
        title="Deployment complete",
        description="",
    )
    grouped = {"demo": [informational, actionable]}
    calls = {"annotate": 0}

    def annotate(item, *, known_projects):
        calls["annotate"] += 1
        item.triage_label = "annotated"
        return item

    monkeypatch.setattr(
        cockpit_inbox,
        "_inbox_db_sources",
        lambda _config: [("demo", tmp_path / "state.db", tmp_path)],
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.inbox_tasks_grouped",
        lambda _config: grouped,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.inbox_tasks_for_project",
        lambda rows, _config, key: list(rows.get(key, [])),
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.open_messages",
        lambda _config, *, known_projects: [],
    )
    monkeypatch.setattr(
        "pollypm.cockpit_inbox_items.annotate_inbox_entry",
        annotate,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_inbox_items._filter_approved_plan_reviews",
        lambda items, **_kwargs: list(items),
    )

    config = SimpleNamespace(projects={"demo": SimpleNamespace()})
    result = cockpit_inbox._pm_inbox_awaits_user_list_uncached(config)

    assert [item.task_id for item in result] == ["demo/1"]
    assert result[0].triage_label == "annotated"
    assert calls["annotate"] == 1


def test_dashboard_count_can_bypass_state_cache(monkeypatch) -> None:
    from pollypm import cockpit_inbox

    monkeypatch.setattr(
        cockpit_inbox,
        "_maybe_cache_count_awaits_user",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("state cache should not be touched")
        ),
    )
    monkeypatch.setattr(
        cockpit_inbox,
        "_pm_inbox_awaits_user_list_uncached",
        lambda _config: [object(), object()],
    )
    cockpit_inbox._AWAITS_USER_COUNT_CACHE.clear()

    count = cockpit_inbox._count_inbox_tasks_for_label(
        SimpleNamespace(),
        use_state_cache=False,
    )

    assert count == 2
