"""Focused perf-regression tests for dashboard inbox rollups."""

from __future__ import annotations

from pathlib import Path
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
