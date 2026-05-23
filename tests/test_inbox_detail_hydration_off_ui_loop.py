"""Regression tests for #1969 — inbox detail hydration must not block
the Textual UI loop.

#1959 deferred the synchronous ``_render_detail`` by one event-loop
tick via ``set_timer(0)``, but the timer callback still ran the
work-service open + task/replies/context fetch + inline-review-
artifact disk read inline on the UI thread. On a project with a cold
sqlite cache that costs 100-400ms of blocked keypresses while the
j/k cursor input piles up behind the Textual loop.

The fix moves the IO-heavy fetches onto
``run_worker(thread=True, exclusive=True)`` and applies the result
via ``call_from_thread`` with a stale-guard so a worker callback
whose target was navigated away cannot paint stale data over the
freshly-selected row.

These tests exercise the unit-level invariants of that fix:

* ``_schedule_deferred_detail_hydration`` records the dispatched
  ``task_id``, paints the placeholder, and calls ``run_worker``
  (instead of invoking ``_render_detail`` inline).
* The off-thread worker resolves the work service + fetches data
  + dispatches the apply via ``call_from_thread`` — the UI thread
  is never blocked.
* The applier honours the stale-guard: if ``_selected_task_id``
  shifts mid-fetch, the result is discarded.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_inbox_app(tmp_path: Path):
    """Construct a ``PollyInboxApp`` via ``__new__`` with the
    minimum attribute surface the hydration codepath touches.

    Mirrors the pattern other ``test_cockpit.py`` cases use to drive
    pure method-level invariants without booting the full Textual
    harness.
    """
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp.__new__(PollyInboxApp)
    app.config_path = tmp_path / "pollypm.toml"
    app._selected_task_id = None
    app._selected_row_key = None
    app._pending_detail_hydration_task_id = None
    app._tasks = []
    app._replies_by_task = {}
    app._unread_ids = set()
    app._plan_review_meta = {}
    app._plan_review_round_trip = {}
    app._blocking_question_meta = {}
    app._proposal_specs = {}
    app._rollup_items = []
    app._rollup_expanded = set()
    app._rollup_show_all = False
    app._rollup_focused_index = None

    # Capture the most-recent placeholder so the test can assert it
    # was painted synchronously before the worker dispatch.
    detail_writes: list[str] = []

    class _DetailStub:
        def update(self, value: str) -> None:
            detail_writes.append(value)

    app.detail = _DetailStub()
    app._detail_writes = detail_writes  # type: ignore[attr-defined]

    return app


# ---------------------------------------------------------------------------
# Placeholder + dispatch invariants
# ---------------------------------------------------------------------------


def test_schedule_deferred_hydration_dispatches_worker_not_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred-hydration entrypoint paints the placeholder and
    hands off to ``run_worker`` — it must NOT call ``_render_detail``
    inline on the UI thread (the #1959 regression that #1969 fixes).
    """
    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/task-123"

    worker_dispatches: list[dict] = []

    def fake_run_worker(fn, **kwargs):
        worker_dispatches.append({"fn": fn, "kwargs": kwargs})

    app.run_worker = fake_run_worker  # type: ignore[method-assign]

    render_detail_calls: list[str] = []

    def fake_render_detail(task_id: str, **kwargs) -> None:
        render_detail_calls.append(task_id)

    app._render_detail = fake_render_detail  # type: ignore[method-assign]

    app._schedule_deferred_detail_hydration("proj/task-123")

    # Placeholder painted synchronously.
    assert app._detail_writes == ["[dim]Loading detail…[/dim]"]

    # Worker dispatched — exclusive thread-mode in the inbox group.
    assert len(worker_dispatches) == 1
    kwargs = worker_dispatches[0]["kwargs"]
    assert kwargs.get("thread") is True
    assert kwargs.get("exclusive") is True
    assert kwargs.get("group") == "inbox_detail_hydrate"

    # CRITICAL: ``_render_detail`` was NOT called inline.
    assert render_detail_calls == []

    # Pending task id recorded so a superseded callback can drop.
    assert app._pending_detail_hydration_task_id == "proj/task-123"


def test_schedule_deferred_hydration_falls_back_when_worker_dispatch_fails(
    tmp_path: Path,
) -> None:
    """If ``run_worker`` raises (no event loop, teardown), we still
    paint a useful detail rather than leaving the user stranded on
    the loading placeholder — match the #1959 fallback shape.
    """
    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/task-789"

    def boom_run_worker(*args, **kwargs):
        raise RuntimeError("no event loop")

    app.run_worker = boom_run_worker  # type: ignore[method-assign]

    fallback_calls: list[str] = []

    def fake_render_detail(task_id: str, **kwargs) -> None:
        fallback_calls.append(task_id)

    app._render_detail = fake_render_detail  # type: ignore[method-assign]

    app._schedule_deferred_detail_hydration("proj/task-789")

    # Placeholder still painted, then synchronous fallback fires.
    assert app._detail_writes == ["[dim]Loading detail…[/dim]"]
    assert fallback_calls == ["proj/task-789"]


# ---------------------------------------------------------------------------
# Worker thread does not touch the UI directly
# ---------------------------------------------------------------------------


def test_hydrate_detail_worker_routes_through_call_from_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker body must dispatch every UI mutation via
    ``call_from_thread``; direct widget calls from a thread would
    race the Textual loop.
    """
    from pollypm.cockpit_ui import PollyInboxApp

    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/task-456"
    app._pending_detail_hydration_task_id = "proj/task-456"

    item = SimpleNamespace(
        task_id="proj/task-456",
        project="proj",
        description="x",
        title="y",
        labels=[],
        roles={},
    )
    # Make ``_item_for_id`` return our item without scanning ``_tasks``.
    app._item_for_id = lambda _id: item  # type: ignore[method-assign]
    app._project_key_is_unknown = lambda _key: False  # type: ignore[method-assign]

    # ``is_task_inbox_entry`` is module-level — patch it for this test.
    monkeypatch.setattr(
        "pollypm.cockpit_ui.is_task_inbox_entry",
        lambda _item: True,
    )

    svc = MagicMock()
    svc.get.return_value = SimpleNamespace(
        task_id="proj/task-456",
        title="hydrated",
        description="body",
        project="proj",
        priority=SimpleNamespace(value="normal"),
        labels=[],
        roles={},
        updated_at=None,
        created_at=None,
        triage_bucket="action",
    )
    svc.list_replies.return_value = []
    svc.get_context.return_value = []
    app._resolve_inbox_svc = lambda *_a, **_kw: svc  # type: ignore[method-assign]

    # Avoid the disk-touching review artifact path in this unit test.
    app._render_inline_review_artifact = lambda _task: None  # type: ignore[method-assign]

    call_from_thread_dispatches: list[tuple] = []

    def fake_call_from_thread(fn, *args, **kwargs):
        call_from_thread_dispatches.append((fn, args, kwargs))

    app.call_from_thread = fake_call_from_thread  # type: ignore[method-assign]

    app._hydrate_detail_worker("proj/task-456")

    # Exactly one dispatch — the success path applier.
    assert len(call_from_thread_dispatches) == 1
    fn, args, _ = call_from_thread_dispatches[0]
    assert fn == app._apply_hydrated_detail
    # task_id is the first positional arg passed through.
    assert args[0] == "proj/task-456"
    # The svc was opened + closed cleanly.
    assert svc.close.called


def test_hydrate_detail_worker_short_circuits_when_selection_changed(
    tmp_path: Path,
) -> None:
    """Stale-guard at worker entry: if the user already moved away,
    the worker bails before opening the work service.
    """
    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/task-NEW"
    app._pending_detail_hydration_task_id = "proj/task-OLD"

    resolve_calls: list = []
    app._resolve_inbox_svc = lambda *a, **kw: resolve_calls.append(a) or None  # type: ignore[method-assign]

    call_from_thread_dispatches: list[tuple] = []
    app.call_from_thread = lambda fn, *a, **kw: call_from_thread_dispatches.append((fn, a))  # type: ignore[method-assign]

    app._hydrate_detail_worker("proj/task-OLD")

    # No svc resolve attempted; no UI applier scheduled.
    assert resolve_calls == []
    assert call_from_thread_dispatches == []


# ---------------------------------------------------------------------------
# Applier honours the stale-guard
# ---------------------------------------------------------------------------


def test_apply_hydrated_detail_discards_when_selection_changed(
    tmp_path: Path,
) -> None:
    """If the selection shifts between worker dispatch and applier
    execution, the result is dropped — no stale paint over the
    freshly-selected row.
    """
    app = _make_inbox_app(tmp_path)
    # Simulate the user navigated to a different row mid-fetch.
    app._selected_task_id = "proj/task-NEW"
    app._pending_detail_hydration_task_id = "proj/task-OLD"

    # If the applier guards correctly it never touches these helpers.
    app._set_reply_mode_for_task = MagicMock()  # type: ignore[method-assign]
    app._detail_build_sections = MagicMock()  # type: ignore[method-assign]
    app._detail_append_thread = MagicMock()  # type: ignore[method-assign]
    app._detail_apply_label_hints = MagicMock()  # type: ignore[method-assign]
    app._render_rollup_items = MagicMock()  # type: ignore[method-assign]

    app._apply_hydrated_detail(
        "proj/task-OLD",
        item=SimpleNamespace(),
        task=SimpleNamespace(),
        replies=[],
        rollup_items_raw=[],
        review_block=None,
    )

    app._set_reply_mode_for_task.assert_not_called()
    app._detail_build_sections.assert_not_called()
    app._detail_append_thread.assert_not_called()
    app._detail_apply_label_hints.assert_not_called()
    app._render_rollup_items.assert_not_called()
    # detail.update never invoked either.
    assert app._detail_writes == []


def test_apply_hydrated_detail_renders_when_selection_still_matches(
    tmp_path: Path,
) -> None:
    """Happy path: selection still on the dispatched task — the
    applier builds sections and updates the detail pane.
    """
    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/task-OK"
    app._pending_detail_hydration_task_id = "proj/task-OK"

    app._set_reply_mode_for_task = MagicMock()  # type: ignore[method-assign]
    app._detail_build_sections = MagicMock(return_value=["header", "body"])  # type: ignore[method-assign]
    app._detail_append_thread = MagicMock()  # type: ignore[method-assign]
    app._detail_apply_label_hints = MagicMock()  # type: ignore[method-assign]
    app._render_rollup_items = MagicMock()  # type: ignore[method-assign]
    app.query_one = MagicMock(side_effect=Exception("no UI"))  # type: ignore[method-assign]

    app._apply_hydrated_detail(
        "proj/task-OK",
        item=SimpleNamespace(),
        task=SimpleNamespace(),
        replies=[],
        rollup_items_raw=[],
        review_block="review-text",
    )

    app._set_reply_mode_for_task.assert_called_once()
    app._detail_build_sections.assert_called_once()
    app._detail_append_thread.assert_called_once()
    app._detail_apply_label_hints.assert_called_once()
    app._render_rollup_items.assert_called_once_with([])
    # Detail update painted the joined sections, with review_block
    # appended under its banner.
    assert len(app._detail_writes) == 1
    painted = app._detail_writes[0]
    assert "header" in painted
    assert "review artifact" in painted
    assert "review-text" in painted


# ---------------------------------------------------------------------------
# Plan-review precomputed-payload invariants (Codex review on PR #2077)
# ---------------------------------------------------------------------------


def test_plan_review_hydration_precomputes_disk_and_store_lookups_off_ui_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan-review hydration must NOT call ``_plan_content_for_review``
    or ``_plan_review_discussion_marker_exists`` from the UI-thread
    applier — both have to be precomputed inside the worker so
    ``_apply_hydrated_detail`` stays pure formatting.

    Regression for the Codex review comment on PR #2077: even after
    #1969 deferred the work-service open + task fetch, the
    plan-review detail still leaked filesystem (canonical plan
    markdown load + config load) and store (work-service
    ``get_context``) calls into the section-builder + label-hint
    applier when the worker callback ran.
    """
    from pollypm import cockpit_ui as cockpit_ui_module
    from pollypm.cockpit_ui import PollyInboxApp

    app = _make_inbox_app(tmp_path)
    app._selected_task_id = "proj/plan-task-1"
    app._pending_detail_hydration_task_id = "proj/plan-task-1"

    item = SimpleNamespace(
        task_id="proj/plan-task-1",
        project="proj",
        description="plan body",
        title="Plan review",
        labels=["plan_review", "project:proj"],
        roles={"requester": "user"},
    )
    app._item_for_id = lambda _id: item  # type: ignore[method-assign]
    app._project_key_is_unknown = lambda _key: False  # type: ignore[method-assign]
    monkeypatch.setattr(
        "pollypm.cockpit_ui.is_task_inbox_entry",
        lambda _item: True,
    )

    plan_task = SimpleNamespace(
        task_id="proj/plan-task-1",
        title="Plan review",
        description="plan body",
        project="proj",
        priority=SimpleNamespace(value="normal"),
        labels=["plan_review", "project:proj"],
        roles={"requester": "user"},
        updated_at=None,
        created_at=None,
        triage_bucket="action",
    )

    # Work service: ``get_context`` is invoked from the worker for
    # both the rollup-items branch (skipped — task is not a rollup)
    # and the discussion-marker branch (the precompute we're verifying
    # lives off the UI loop). Mock returns [] so the marker resolves
    # to False without raising.
    svc = MagicMock()
    svc.get.return_value = plan_task
    svc.list_replies.return_value = []
    svc.get_context.return_value = []
    app._resolve_inbox_svc = lambda *_a, **_kw: svc  # type: ignore[method-assign]
    app._render_inline_review_artifact = lambda _task: None  # type: ignore[method-assign]

    # Avoid the rollup branch entirely.
    monkeypatch.setattr(
        "pollypm.cockpit_ui._task_is_rollup", lambda _t: False,
    )

    # Counter-stubs for the two helpers Codex flagged. We assert these
    # are touched ONLY during the worker run (not during the applier).
    plan_content_calls: list[tuple] = []

    def fake_plan_content_for_review(meta, config_path):
        plan_content_calls.append((dict(meta or {}), config_path))
        return "## plan body precomputed off UI loop"

    # ``_plan_content_for_review`` is imported into ``cockpit_ui`` at
    # module load time — patch the binding callers actually resolve.
    monkeypatch.setattr(
        cockpit_ui_module,
        "_plan_content_for_review",
        fake_plan_content_for_review,
    )

    discussion_marker_calls: list[tuple] = []

    def fake_discussion_marker_exists(_self, _item, _task_id):
        discussion_marker_calls.append((_item, _task_id))
        return False

    monkeypatch.setattr(
        PollyInboxApp,
        "_plan_review_discussion_marker_exists",
        fake_discussion_marker_exists,
    )

    # Capture the payload the worker dispatches to the applier so we
    # can replay it without going through ``call_from_thread``.
    dispatches: list[tuple] = []
    app.call_from_thread = lambda fn, *a, **kw: dispatches.append((fn, a, kw))  # type: ignore[method-assign]

    # --- WORKER PHASE -----------------------------------------------------
    app._hydrate_detail_worker("proj/plan-task-1")

    # Worker must have precomputed the plan content off the UI loop.
    assert len(plan_content_calls) == 1, (
        "expected _plan_content_for_review to fire exactly once — "
        "from the worker thread, precomputing the plan body for the "
        "applier"
    )
    # Worker must have precomputed the discussion-marker via the work
    # service — replies are empty so the round-trip heuristic fell
    # through to the store lookup. The worker uses ``svc.get_context``
    # directly (not the helper method on the app) to avoid re-opening
    # a second work service, so we assert the svc surface here AND
    # confirm the helper method itself was NEVER reached on either
    # the worker OR the applier path.
    marker_get_context_calls = [
        call for call in svc.get_context.call_args_list
        if call.kwargs.get("entry_type") == "plan_review_discussed"
    ]
    assert len(marker_get_context_calls) == 1, (
        "expected exactly one svc.get_context call with the plan-"
        "review discussion entry-type — the worker's precompute"
    )
    assert discussion_marker_calls == [], (
        "the dedicated _plan_review_discussion_marker_exists helper "
        "must NOT be invoked from either the worker or the applier "
        "(the worker calls svc.get_context directly to avoid opening "
        "a second work service)"
    )

    # Exactly one dispatch — the success-path applier — with the
    # precomputed plan content + discussion-marker bool in the
    # positional payload.
    assert len(dispatches) == 1
    fn, args, _ = dispatches[0]
    assert fn == app._apply_hydrated_detail
    # Positional layout: task_id, item, task, replies, rollup, review_block,
    # plan_content_precomputed, discussion_marker_precomputed
    assert args[0] == "proj/plan-task-1"
    assert args[6] == "## plan body precomputed off UI loop"
    assert args[7] is False

    # --- APPLIER PHASE ----------------------------------------------------
    # Reset counters so the next phase's assertions only see calls
    # made by the applier itself.
    plan_content_calls.clear()
    discussion_marker_calls.clear()
    svc.get_context.reset_mock()

    # Stub helpers the applier reaches that aren't relevant to the
    # disk/store invariant we're asserting. We replace the section
    # builder + label-hint applier with capture-stubs so we can
    # assert the precomputed payload reached them — and so the
    # underlying ``_format_sender`` / ``_resolve_pm_target`` chain
    # (which expects more attrs than this SimpleNamespace carries)
    # does not run inside the unit test.
    build_section_calls: list[dict] = []

    def fake_build_sections(_task, **kwargs):
        build_section_calls.append(kwargs)
        return ["[plan-review-header]", kwargs.get("plan_content_precomputed", "")]

    app._detail_build_sections = fake_build_sections  # type: ignore[method-assign]

    label_hint_calls: list[dict] = []

    def fake_label_hints(_task, _task_id, _item, _replies, **kwargs):
        label_hint_calls.append(kwargs)
        # Mirror the real applier's round-trip state write so we can
        # assert the precomputed bool flowed through, not a re-lookup.
        marker = kwargs.get("discussion_marker_precomputed")
        if marker is not None:
            app._plan_review_round_trip[_task_id] = bool(marker)

    app._detail_apply_label_hints = fake_label_hints  # type: ignore[method-assign]

    app._render_rollup_items = MagicMock()  # type: ignore[method-assign]
    app.query_one = MagicMock(side_effect=Exception("no UI in unit test"))  # type: ignore[method-assign]
    app._set_reply_mode_for_task = MagicMock()  # type: ignore[method-assign]

    # Replay the worker's payload synchronously — this is what
    # ``call_from_thread`` would do on the UI loop.
    fn(*args)

    # CRITICAL: neither disk nor store helper is touched during the
    # applier. All the plan-review content + round-trip state came
    # from the precomputed payload.
    assert plan_content_calls == [], (
        "_plan_content_for_review fired during _apply_hydrated_detail "
        "— plan-review hydration is leaking filesystem IO back onto "
        "the Textual UI loop"
    )
    assert discussion_marker_calls == [], (
        "_plan_review_discussion_marker_exists fired during "
        "_apply_hydrated_detail — plan-review hydration is leaking "
        "work-service IO back onto the Textual UI loop"
    )
    marker_get_context_calls_during_apply = [
        call for call in svc.get_context.call_args_list
        if call.kwargs.get("entry_type") == "plan_review_discussed"
    ]
    assert marker_get_context_calls_during_apply == [], (
        "svc.get_context for the plan-review discussion marker fired "
        "during the applier — the worker's precompute is being "
        "bypassed"
    )

    # The applier forwarded the precomputed payload into the section
    # builder + label-hint applier.
    assert len(build_section_calls) == 1
    assert (
        build_section_calls[0].get("plan_content_precomputed")
        == "## plan body precomputed off UI loop"
    )
    assert len(label_hint_calls) == 1
    assert label_hint_calls[0].get("discussion_marker_precomputed") is False

    # Applier produced output and surfaced the precomputed plan body
    # in the rendered detail pane.
    assert len(app._detail_writes) == 1
    painted = app._detail_writes[0]
    assert "plan body precomputed off UI loop" in painted

    # Round-trip state was set from the precomputed bool (False here:
    # no replies, store-marker absent), not by re-running the lookup.
    assert app._plan_review_round_trip.get("proj/plan-task-1") is False
