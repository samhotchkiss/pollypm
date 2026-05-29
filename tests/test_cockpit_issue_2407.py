from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_pending_approve_undo_morph_survives_task_detail_rerender(
    monkeypatch,
) -> None:
    import pollypm.cockpit_tasks as cockpit_tasks

    app = cockpit_tasks.PollyTasksApp(Path("/tmp/pollypm.toml"), "demo")
    monkeypatch.setattr(cockpit_tasks, "_render_overview", lambda *a, **k: "overview")
    monkeypatch.setattr(cockpit_tasks, "_render_context", lambda _task: "context")
    monkeypatch.setattr(cockpit_tasks, "_render_live", lambda *a, **k: "live")
    monkeypatch.setattr(cockpit_tasks, "_status_label", lambda *a, **k: "review")
    monkeypatch.setattr(cockpit_tasks, "_status_display_label", lambda status: status)
    monkeypatch.setattr(cockpit_tasks, "priority_glyph", lambda _task: "")
    app._render_timeline = lambda _executions: None  # type: ignore[method-assign]

    task = SimpleNamespace(
        task_id="demo/1",
        task_number=1,
        title="Review me",
        work_status=SimpleNamespace(value="review"),
        updated_at=None,
        created_at=None,
        executions=[],
    )
    app._tasks = [task]
    app._pending_review_action = cockpit_tasks._PendingReviewAction(
        task_ids=("demo/1",),
        task_numbers=(1,),
        decision="approve",
        reason=None,
        deadline=cockpit_tasks.monotonic() + 5.0,
    )

    app._render_selected_task(task, owner=None, flow=None, active_session=None)

    assert str(app.approve_button.label).startswith("Undo (")
    assert app.approve_button.has_class("-undo") is True
    assert app.approve_button.disabled is False
    assert app.reject_button.disabled is True


def test_keyboard_approve_reject_with_search_filter_use_destructive_arming(
    monkeypatch,
) -> None:
    import pollypm.cockpit_tasks as cockpit_tasks

    app = cockpit_tasks.PollyTasksApp(Path("/tmp/pollypm.toml"), "demo")
    app._selected_task_id = "demo/1"
    app._search_query = "already filtered"
    focus_calls: list[None] = []
    approve_fire_calls: list[None] = []
    reject_fire_calls: list[None] = []
    arm_calls: list[tuple[str, str | None]] = []

    app.action_focus_search = lambda: focus_calls.append(None)  # type: ignore[method-assign]
    app._fire_approve_task = lambda: approve_fire_calls.append(None)  # type: ignore[method-assign]
    app._fire_reject_task = lambda: reject_fire_calls.append(None)  # type: ignore[method-assign]
    app._notify_arm_hint = lambda _label: None  # type: ignore[method-assign]

    def fake_destructive_action_safe(_app, action, *, target_id, selection):
        arm_calls.append((action, target_id))
        return False

    monkeypatch.setattr(
        cockpit_tasks,
        "destructive_action_safe",
        fake_destructive_action_safe,
    )

    app.action_approve_task()
    app.action_reject_task()

    assert focus_calls == []
    assert approve_fire_calls == []
    assert reject_fire_calls == []
    assert arm_calls == [("approve_task", "demo/1"), ("reject_task", "demo/1")]


def test_keyboard_approve_reject_redirect_when_search_input_has_focus() -> None:
    import pollypm.cockpit_tasks as cockpit_tasks

    app = cockpit_tasks.PollyTasksApp(Path("/tmp/pollypm.toml"), "demo")
    app._selected_task_id = "demo/1"
    app.search_input = SimpleNamespace(has_focus=True)  # type: ignore[assignment]
    focus_calls: list[None] = []
    approve_fire_calls: list[None] = []
    reject_fire_calls: list[None] = []

    app.action_focus_search = lambda: focus_calls.append(None)  # type: ignore[method-assign]
    app._fire_approve_task = lambda: approve_fire_calls.append(None)  # type: ignore[method-assign]
    app._fire_reject_task = lambda: reject_fire_calls.append(None)  # type: ignore[method-assign]

    app.action_approve_task()
    app.action_reject_task()

    assert focus_calls == [None, None]
    assert approve_fire_calls == []
    assert reject_fire_calls == []


def test_inbox_selected_ignores_stale_row(monkeypatch) -> None:
    import pollypm.cockpit_ui as cockpit_ui

    class FakeInboxListItem:
        def __init__(self, row_ref):
            self.row_ref = row_ref
            self.task_id = row_ref.task_id

    monkeypatch.setattr(cockpit_ui, "_InboxListItem", FakeInboxListItem)
    app = cockpit_ui.PollyInboxApp.__new__(cockpit_ui.PollyInboxApp)
    current_ref = SimpleNamespace(key="current", task_id="demo/current")
    stale_ref = SimpleNamespace(key="stale", task_id="demo/stale")
    app._visible_rows = [current_ref]
    app._selected_task_id = "demo/current"
    app._selected_row_key = "current"
    render_calls: list[str] = []
    read_calls: list[str] = []
    app._render_detail = lambda task_id: render_calls.append(task_id)  # type: ignore[method-assign]
    app._mark_open_read = lambda task_id: read_calls.append(task_id)  # type: ignore[method-assign]

    event = SimpleNamespace(item=FakeInboxListItem(stale_ref))

    cockpit_ui.PollyInboxApp._on_row_selected(app, event)

    assert app._selected_task_id == "demo/current"
    assert app._selected_row_key == "current"
    assert render_calls == []
    assert read_calls == []
