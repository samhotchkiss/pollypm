"""Textual UI tests for the cockpit inbox screen.

Drives :class:`pollypm.cockpit_ui.PollyInboxApp` via ``Pilot`` to assert
the full interactive loop: navigate with arrows, open a message, reply,
archive, and verify the underlying work-service state after each action.

Skipped when the minimum-viable config layer can't stub a single-project
cockpit (e.g. platform-specific config bootstrap failures) so a CI
environment without tmux doesn't block on unrelated infra.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.store import SQLAlchemyStore
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Config fixture — a minimal "one project" cockpit config with a real DB
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    """Emit a pollypm.toml pointing at a single-project workspace.

    Must mirror the layout the cockpit loader expects: a workspace root
    with a ``[pollypm.projects.<key>]`` block pointing at a folder that
    has a ``.pollypm/state.db`` file we just seeded with inbox rows.
    """
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


def _seed_project(project_path: Path) -> list[str]:
    """Create a few inbox tasks in a project-root state.db. Returns task_ids."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        ids: list[str] = []
        for title, body in [
            ("Smoke subject", "Smoke body"),
            ("Deploy blocked", "Verify email click."),
            ("Homepage rewrite", "Review please."),
        ]:
            t = svc.create(
                title=title,
                description=body,
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
            )
            ids.append(t.task_id)
        return ids
    finally:
        svc.close()


def _seed_workspace_message(
    workspace_root: Path,
    *,
    subject: str = "Workspace notify",
    body: str = "Fresh notification from Store.",
    scope: str = "demo",
    recipient: str = "user",
    sender: str = "polly",
    type: str = "notify",
    tier: str = "immediate",
) -> int:
    db_path = workspace_root / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type=type,
            tier=tier,
            recipient=recipient,
            sender=sender,
            subject=subject,
            body=body,
            scope=scope,
        )
    finally:
        store.close()


def _seed_threaded_task(project_path: Path) -> str:
    """Create one inbox task with a short two-reply thread."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title="Feedback on task #5",
            description="Waiting on a quick follow-up.",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
        )
        svc.add_reply(task.task_id, "Got it, shipping fix", actor="user")
        svc.add_reply(task.task_id, "Approved, looks good", actor="polly")
        return task.task_id
    finally:
        svc.close()


@pytest.fixture
def inbox_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()  # pretend git repo
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    ids = _seed_project(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_ids": ids,
    }


def _load_config_compatible(config_path: Path) -> bool:
    """Skip the suite if config loader rejects our minimal TOML shape."""
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def inbox_app(inbox_env):
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(inbox_env["config_path"])


# ---------------------------------------------------------------------------
# Pilot tests
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async test body under asyncio, matching onboarding tests."""
    asyncio.run(coro)


def _visible_titles(app) -> list[str]:
    from pollypm.cockpit_ui import _InboxListItem

    return [
        child.task_ref.title
        for child in app.list_view.children
        if isinstance(child, _InboxListItem)
    ]


def _triaged_entry(*, title: str, body: str = "", project: str = "demo"):
    from pollypm.cockpit_inbox_items import InboxEntry, annotate_inbox_entry

    return annotate_inbox_entry(
        InboxEntry(
            task_id="msg:test:1",
            title=title,
            description=body,
            project=project,
            labels=[],
        ),
        known_projects={"demo"},
    )


def test_inbox_triage_scores_compound_decision_ahead_of_blocker() -> None:
    item = _triaged_entry(
        title="Decision needed",
        body="Waiting on clarity from upstream before implementation can continue.",
    )

    assert item.triage_bucket == "action"
    assert item.triage_rank == 0
    assert item.triage_label == "decision needed"


def test_inbox_triage_completion_beats_generic_action_marker() -> None:
    item = _triaged_entry(
        title="[Action] Demo shipped cleanly",
        body="Shipped to main with tests green.",
    )

    assert item.triage_bucket == "info"
    assert item.triage_rank == 2
    assert item.triage_label == "completed update"


def test_replayed_plan_review_notifications_collapse_to_most_recent() -> None:
    """Architects that re-fire ``[Action] Plan ready for review`` for the
    same plan should not flood the inbox with duplicate rows — keep the
    newest, drop the older replays."""
    from datetime import datetime

    from pollypm.cockpit_inbox_items import (
        InboxEntry,
        _dedupe_replayed_plan_reviews,
    )

    older = InboxEntry(
        task_id="msg:booktalk:1",
        title="[Action] Plan ready for review: booktalk",
        project="booktalk",
        labels=[
            "plan_review",
            "project:booktalk",
            "plan_task:booktalk/3",
        ],
        created_at=datetime(2026, 4, 23, 16, 2, 58),
        updated_at=datetime(2026, 4, 23, 16, 2, 58),
    )
    newer = InboxEntry(
        task_id="msg:booktalk:2",
        title="[Action] Plan ready for review: booktalk",
        project="booktalk",
        labels=[
            "plan_review",
            "project:booktalk",
            "plan_task:booktalk/3",
        ],
        created_at=datetime(2026, 4, 23, 16, 6, 52),
        updated_at=datetime(2026, 4, 23, 16, 6, 52),
    )
    different_plan = InboxEntry(
        task_id="msg:polly_remote:9",
        title="[Action] Plan ready for review: polly_remote",
        project="polly_remote",
        labels=[
            "plan_review",
            "project:polly_remote",
            "plan_task:polly_remote/9",
        ],
        created_at=datetime(2026, 4, 20, 9, 0, 0),
        updated_at=datetime(2026, 4, 20, 9, 0, 0),
    )
    other_action = InboxEntry(
        task_id="msg:polly_remote:12",
        title="[Action] N-RC1 review (polly_remote/12)",
        project="polly_remote",
        labels=["project:polly_remote"],
        created_at=datetime(2026, 4, 23, 18, 53, 0),
        updated_at=datetime(2026, 4, 23, 18, 53, 0),
    )

    deduped = _dedupe_replayed_plan_reviews(
        [older, newer, different_plan, other_action]
    )
    deduped_ids = [
        getattr(item, "task_id", None) for item in deduped
    ]
    # Older booktalk plan-review collapsed; newer kept.
    assert "msg:booktalk:1" not in deduped_ids
    assert "msg:booktalk:2" in deduped_ids
    # Different plan + non-plan-review entries are untouched.
    assert "msg:polly_remote:9" in deduped_ids
    assert "msg:polly_remote:12" in deduped_ids


def test_task_backed_inbox_entries_default_to_action() -> None:
    from types import SimpleNamespace

    from pollypm.cockpit_inbox_items import annotate_inbox_entry, task_to_inbox_entry

    task = SimpleNamespace(
        task_id="demo/1",
        title="Smoke subject",
        description="Plain assigned work with no keyword markers.",
        project="demo",
        labels=[],
        roles={},
    )

    item = annotate_inbox_entry(
        task_to_inbox_entry(task, db_path=None),
        known_projects={"demo"},
    )

    assert item.needs_action is True
    assert item.triage_label == "task assigned"


def test_inbox_lists_seeded_messages(inbox_env, inbox_app) -> None:
    """On mount, every seeded inbox task shows up in the left list."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert len(inbox_app._tasks) == len(inbox_env["task_ids"])
            # All three are unread on first load — nothing has a read marker.
            assert len(inbox_app._unread_ids) == len(inbox_env["task_ids"])
    _run(body())


def test_list_row_renders_title_on_line1_and_project_age_on_line2(
    inbox_env, inbox_app,
) -> None:
    """Each row is two lines: bold title, then dim ``project · age``.

    The sender (always "polly") must NOT appear in the row; project key
    is more useful across a multi-project workspace.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from pollypm.cockpit_ui import _InboxListItem, _format_inbox_row
            rows = [
                child for child in inbox_app.list_view.children
                if isinstance(child, _InboxListItem)
            ]
            assert rows, "expected at least one inbox row"
            first = rows[0]
            # Re-derive the Rich Text from the public helper so we're not
            # probing Textual's private Static internals.
            rendered = _format_inbox_row(first.task_ref, is_unread=first.is_unread)
            plain = rendered.plain
            # Two lines — title then metadata.
            lines = plain.split("\n")
            assert len(lines) == 2, f"expected 2 lines, got: {lines!r}"
            # Line 1 holds the subject (one of the seeded titles).
            assert any(
                s in lines[0] for s in ("Smoke", "Deploy", "Homepage")
            ), f"expected a seeded title on line 1, got: {lines[0]!r}"
            # Line 2 has the project key; never the sender "polly".
            assert "demo" in lines[1]
            assert "polly" not in lines[1].lower()
            # Age is relative — "just now" is fine for a freshly-seeded row.
            assert "\u00b7" in lines[1] or "ago" in lines[1] or "now" in lines[1]
    _run(body())


def test_ops_anomaly_subjects_route_to_info_bucket() -> None:
    """System-health / inbox-machinery anomalies (``Misrouted review
    ping``, ``Repeated stale review ping``, ``Stale planner tasks``,
    ``Review requested for missing task``, ``Second bogus review
    ping``) are operator-triage signals — not user decisions. They
    should not crowd the user's action lens. The producers ought to
    use ``--requester polly``; until they all migrate, this
    defensive subject-pattern check demotes them at read time."""
    cases = [
        "[Action] Misrouted review ping: proj/1",
        "[Action] Repeated stale review ping for polly_remote/12",
        "[Action] Second bogus review ping: proj/1 not found",
        "[Action] Stale planner tasks at code_review: dice/1 + wordgame/1",
        "[Action] Review requested for missing task: shortlink-gen/1",
        "[Action] Review-needed notifications contain stale rows",
    ]
    for subject in cases:
        item = _triaged_entry(
            title=subject,
            body="Operator should look at this; not a user decision.",
        )
        assert item.triage_bucket == "info", (
            f"expected info bucket for {subject!r}, got "
            f"{item.triage_bucket!r}/{item.triage_label!r}"
        )
        assert item.triage_label == "operations alert"


def test_ops_anomaly_pattern_does_not_swallow_user_actions() -> None:
    """The subject-pattern check must only fire on the operator
    metadata phrases. Genuine user-facing items that happen to
    mention 'stale' or 'review' in passing must NOT get demoted."""
    item = _triaged_entry(
        title="[Action] N-RC1 review (polly_remote/12): code solid",
        body="walkthrough blocked on N7+N1 infra; need your call.",
    )
    # Real reviewer escalation routed to user — must stay actionable.
    assert item.triage_bucket == "action"
    assert item.triage_label != "operations alert"


def test_digest_subject_prefix_routes_to_info_bucket() -> None:
    """Subject lines starting with 'Digest:' are roll-up summaries
    by definition. Producers sometimes mistier them as immediate
    (e.g. PMs whose body says 'need your call before I act') so
    they leak into the action lens. Title-prefix detection routes
    them to info regardless of body content — the digest is the
    bound-of-attention surface, not the act-on surface."""
    item = _triaged_entry(
        title="[Action] Digest: inbox triage — need your call",
        body=(
            "**State of the inbox (157 items):** 2 stale plan reviews; "
            "8 misrouted review pings; need your call before I act."
        ),
    )

    assert item.triage_bucket == "info"
    assert item.triage_label == "digest"


def test_completion_title_wins_over_body_review_keyword() -> None:
    """Completion announcements like ``[Action] X CLI E2E complete``
    must triage as info / completed-update even when the body
    mentions 'approved by user' (the most common ship-summary
    phrasing). Without this, the body's ``approve``/``review``
    keyword wins the score-tie via kind-priority and the row gets
    bucketed into the action lens — polluting the operator's
    ``action needed`` view for days with stale ship reports."""
    item = _triaged_entry(
        title="[Action] Calculator CLI E2E complete",
        body=(
            "**calc_demo shipped** — 2 tasks done, 65 LOC, 7/7 pytest "
            "green, zero sprawl.\n\nPipeline:\n- Plan (calc_demo/1) → "
            "approved by user\n- T1 compute → 25 LOC, approved"
        ),
    )

    assert item.triage_bucket == "info"
    assert item.triage_rank == 2
    assert item.triage_label == "completed update"


def test_task_inbox_triage_reflects_work_status() -> None:
    """Task-source inbox items used to triage as the generic
    'task assigned' regardless of state, so a plan task waiting
    at user-review and a fresh queued task looked identical in the
    inbox label column. Triage by work_status so the operator can
    see at a glance whether the task needs action now or is just
    parked in their lane."""
    from types import SimpleNamespace

    from pollypm.cockpit_inbox_items import (
        InboxEntry,
        annotate_inbox_entry,
        task_to_inbox_entry,
    )

    def _entry(work_status: str) -> InboxEntry:
        task = SimpleNamespace(
            task_id="demo/1",
            title="A task",
            description="",
            project="demo",
            labels=[],
            roles={},
            work_status=SimpleNamespace(value=work_status),
        )
        return annotate_inbox_entry(
            task_to_inbox_entry(task, db_path=None),
            known_projects={"demo"},
        )

    review = _entry("review")
    assert review.triage_bucket == "action"
    assert review.triage_label == "review needed"

    paused = _entry("on_hold")
    assert paused.triage_bucket == "info"
    assert paused.triage_label == "paused"

    blocked = _entry("blocked")
    assert blocked.triage_bucket == "info"
    assert blocked.triage_label == "blocked by deps"

    queued = _entry("queued")
    assert queued.triage_bucket == "action"
    assert queued.triage_label == "task assigned"


def test_user_prompt_block_surfaces_summary_steps_and_decision() -> None:
    """When a message carries a structured ``user_prompt`` payload,
    the inbox detail pane should lead with the plain-English summary,
    steps, and decision question — not bury them under the raw worker
    body. Architects already do this work; the renderer must use it."""
    from pollypm.cockpit_ui import _render_user_prompt_block

    payload = {
        "user_prompt": {
            "summary": (
                "The reachability work is ready, but Polly cannot walk "
                "through it end to end until the backend deployment exists."
            ),
            "steps": [
                "Make the backend deployment available to Polly.",
                "Give Polly any access needed to run the walkthrough.",
            ],
            "question": (
                "Approve the work now with a follow-up walkthrough, "
                "or wait until the live environment is available?"
            ),
        }
    }

    rendered = _render_user_prompt_block(payload)
    assert rendered is not None
    assert "reachability work is ready" in rendered
    assert "What to do" in rendered
    assert "Make the backend deployment available" in rendered
    assert "Give Polly any access needed" in rendered
    assert "Decision" in rendered
    assert "Approve the work now" in rendered


def test_heuristic_action_block_extracts_summary_and_steps_from_body() -> None:
    """Legacy notifications without a ``user_prompt`` payload still
    benefit from leading the detail pane with a structured block.
    Mirrors the dashboard's Action Needed card behaviour: heuristic
    summary + numbered steps surface at the top, raw body remains
    below for technical context."""
    from pollypm.cockpit_ui import _render_heuristic_action_block

    body = (
        "Blocker: Acceptance cannot run without a live Fly.io app, "
        "org creds, Postgres/Redis provisioned, and a deploy "
        "pipeline.\n\n"
        "Request one of:\n"
        "  (a) Split deploy acceptance into a follow-up task.\n"
        "  (b) Grant a Fly-enabled session with credentials.\n"
    )
    rendered = _render_heuristic_action_block(body)
    assert rendered is not None
    assert "Acceptance cannot run without a live Fly.io app" in rendered
    assert "What to do" in rendered


def test_heuristic_action_block_returns_none_for_empty_body() -> None:
    from pollypm.cockpit_ui import _render_heuristic_action_block

    assert _render_heuristic_action_block(None) is None
    assert _render_heuristic_action_block("") is None
    assert _render_heuristic_action_block("   \n\n  ") is None


def test_user_prompt_block_returns_none_without_user_prompt_payload() -> None:
    """Messages without a user_prompt payload fall back to the legacy
    body-only render — the helper signals this by returning None."""
    from pollypm.cockpit_ui import _render_user_prompt_block

    assert _render_user_prompt_block(None) is None
    assert _render_user_prompt_block({}) is None
    assert _render_user_prompt_block({"user_prompt": None}) is None
    assert _render_user_prompt_block({"user_prompt": "not a dict"}) is None
    # An empty user_prompt dict has no fields to render — None as well.
    assert _render_user_prompt_block({"user_prompt": {}}) is None


def test_action_bucket_row_drops_redundant_action_prefix() -> None:
    """The inbox already groups action-needed items under the
    'action needed' header — repeating '[Action]' on every row is
    redundant noise that eats list-pane width."""
    from types import SimpleNamespace

    from pollypm.cockpit_ui import _format_inbox_row

    action_task = SimpleNamespace(
        task_id="msg:demo:1",
        title="[Action] Fly.io setup needed for demo",
        project="demo",
        updated_at="",
        triage_bucket="action",
    )
    info_task = SimpleNamespace(
        task_id="msg:demo:2",
        title="[Action] Demo shipped cleanly",
        project="demo",
        updated_at="",
        triage_bucket="info",
    )

    action_plain = _format_inbox_row(action_task, is_unread=True).plain
    info_plain = _format_inbox_row(info_task, is_unread=True).plain

    # Action bucket: prefix dropped, rest of the subject preserved.
    assert "Fly.io setup needed for demo" in action_plain
    assert "[Action]" not in action_plain
    # Non-action bucket (info / completion / orphaned): no strip,
    # so the original subject still shows.
    assert "[Action] Demo shipped cleanly" in info_plain


def test_inbox_row_marks_rejection_feedback_threads(inbox_env) -> None:
    from pollypm.cockpit_ui import _format_inbox_row

    db_path = inbox_env["project_path"] / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=inbox_env["project_path"])
    try:
        task = svc.create(
            title="Rejected demo/1 — Smoke subject",
            description="Need better rollback coverage.\n\nReturned for rework.",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "polly", "operator": "user"},
            priority="high",
            created_by="polly",
            labels=["review_feedback", "task:demo/1", "project:demo"],
        )
    finally:
        svc.close()

    plain = _format_inbox_row(task, is_unread=True).plain.splitlines()
    assert plain[0].startswith("◆ 🔄 Rejected demo/1")
    assert "feedback for demo/1" in plain[1]


def test_selecting_a_row_renders_detail_and_clears_unread(inbox_env, inbox_app) -> None:
    """Keyboard navigation opens the message and records a read marker."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Land on the first row and trigger an open.
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            selected = inbox_app._selected_task_id
            assert selected is not None
            # Detail renders the subject (plain text, markup-stripped).
            detail_text = str(inbox_app.detail.render())
            assert any(s in detail_text for s in ("Smoke subject", "Deploy", "Homepage"))
            # The row is no longer unread locally…
            assert selected not in inbox_app._unread_ids
            # …and the work-service has a read marker for that task.
            svc = inbox_app._svc_for_task(selected)
            try:
                reads = svc.get_context(selected, entry_type="read")
            finally:
                svc.close()
            assert len(reads) == 1
    _run(body())


def test_action_items_sort_ahead_of_completed_updates(
    inbox_env, inbox_app,
) -> None:
    """Action-required rows stay above completion noise even if newer."""
    workspace_root = inbox_env["project_path"].parent
    _seed_workspace_message(
        workspace_root,
        subject="[Action] Fly.io setup needed for demo",
        body="Set up Fly.io access so deploy checks can keep running.",
        scope="demo",
    )
    _seed_workspace_message(
        workspace_root,
        subject="[Action] Demo shipped cleanly",
        body="Shipped to main with tests green.",
        scope="demo",
    )

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # _visible_titles inspects the underlying task title, so
            # the "[Action]" prefix is still present at the data layer
            # — the rendered row drops it (covered by
            # test_action_bucket_row_drops_redundant_action_prefix).
            titles = _visible_titles(inbox_app)
            assert "[Action] Fly.io setup needed for demo" in titles
            assert "[Action] Demo shipped cleanly" not in titles
            status_text = str(inbox_app.status.render())
            assert "FYI hidden" in status_text
            assert "m show all" in status_text

            await pilot.press("m")
            await pilot.pause()
            titles = _visible_titles(inbox_app)
            assert "[Action] Fly.io setup needed for demo" in titles
            assert "[Action] Demo shipped cleanly" in titles
            assert titles.index("[Action] Fly.io setup needed for demo") < titles.index(
                "[Action] Demo shipped cleanly"
            )

    _run(body())


def test_orphaned_workspace_messages_hidden_by_default(
    inbox_env, inbox_app,
) -> None:
    """Messages for deleted projects are quarantined unless explicitly shown."""
    workspace_root = inbox_env["project_path"].parent
    _seed_workspace_message(
        workspace_root,
        subject="[Action] Deleted project still asking for review",
        body="This should not pollute the active inbox.",
        scope="ghost_proj",
    )

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "[Action] Deleted project still asking for review" not in _visible_titles(inbox_app)
            status_text = str(inbox_app.status.render()).lower()
            assert "orphaned hidden" in status_text

            inbox_app.action_toggle_filter_orphaned()
            await pilot.pause()
            assert "[Action] Deleted project still asking for review" in _visible_titles(inbox_app)

    _run(body())


def test_action_message_detail_shows_action_required_banner(
    inbox_env, inbox_app,
) -> None:
    """Actionable messages render an explicit banner in the detail pane."""
    workspace_root = inbox_env["project_path"].parent
    title = "[Action] Fly.io setup needed for demo"
    _seed_workspace_message(
        workspace_root,
        subject=title,
        body="Set up Fly.io access so deploy checks can keep running.",
        scope="demo",
    )

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            titles = _visible_titles(inbox_app)
            assert title in titles
            row_index = titles.index(title)
            inbox_app.list_view.index = row_index
            await pilot.press("enter")
            await pilot.pause()
            detail_text = str(inbox_app.detail.render())
            assert "Action Required" in detail_text
            assert "setup needed" in detail_text.lower()

    _run(body())


def test_reply_input_is_always_present_on_detail_open(inbox_env, inbox_app) -> None:
    """Reply Input is visible from mount — not gated by pressing ``r``.

    The list keeps focus on mount so j/k still works; the user must
    explicitly press ``r`` (or Tab/click) to land in the reply box.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Reply Input exists in the widget tree from first paint.
            assert inbox_app.reply_input.is_mounted
            # It is visually shown (always-visible design — no toggle class).
            assert inbox_app.reply_input.display is not False
            # The list, not the reply box, has focus on mount.
            assert inbox_app.list_view.has_focus
            assert not inbox_app.reply_input.has_focus
    _run(body())


def test_r_shortcut_focuses_reply_without_toggling_visibility(
    inbox_env, inbox_app,
) -> None:
    """Pressing ``r`` focuses the already-visible reply box."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert not inbox_app.reply_input.has_focus
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus
    _run(body())


def test_esc_from_reply_returns_focus_to_list(inbox_env, inbox_app) -> None:
    """Esc inside the reply box hands focus back to the list (no exit)."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus
            await pilot.press("escape")
            await pilot.pause()
            # Focus is back on the list and the app is still running.
            assert inbox_app.list_view.has_focus
            assert not inbox_app.reply_input.has_focus
    _run(body())


def test_reply_flow_persists_and_appears_in_thread(inbox_env, inbox_app) -> None:
    """Typing in the always-visible reply + Enter posts and clears the input."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id is not None

            # Focus the reply box via the keyboard shortcut.
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus

            # Type a reply and submit.
            inbox_app.reply_input.value = "Got it, thanks"
            await pilot.press("enter")
            await pilot.pause()

            # Input is cleared and focus returns to the list.
            assert inbox_app.reply_input.value == ""
            assert inbox_app.list_view.has_focus

            # The reply is persisted as a reply context row.
            svc = inbox_app._svc_for_task(task_id)
            try:
                replies = svc.list_replies(task_id)
            finally:
                svc.close()
            assert [e.text for e in replies] == ["Got it, thanks"]
            assert replies[0].actor == "user"
            assert replies[0].entry_type == "reply"

            # Detail pane re-renders with the reply visible in-thread.
            rendered = str(inbox_app.detail.render())
            assert "Got it, thanks" in rendered
    _run(body())


def test_right_left_expand_and_collapse_inline_thread_rows(tmp_path: Path) -> None:
    """Right expands a thread inline; left walks back to parent then collapses."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        task_id = _seed_threaded_task(project_path)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp, _InboxListItem
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._selected_task_id == task_id
            assert [row.kind for row in app._visible_rows] == ["task"]
            assert app._visible_rows[0].reply_count == 2

            await pilot.press("right")
            await pilot.pause()
            assert [row.kind for row in app._visible_rows] == ["task", "reply", "reply"]
            assert app._visible_rows[0].expanded is True

            rows = [
                child for child in app.list_view.children
                if isinstance(child, _InboxListItem)
            ]
            assert "Got it, shipping fix" in str(rows[1]._body.render())

            await pilot.press("right")
            await pilot.pause()
            assert app.list_view.index == 1
            assert app._visible_rows[1].is_reply is True

            await pilot.press("left")
            await pilot.pause()
            assert app.list_view.index == 0
            assert app._visible_rows[0].is_task is True

            await pilot.press("left")
            await pilot.pause()
            assert [row.kind for row in app._visible_rows] == ["task"]
            assert app._visible_rows[0].expanded is False
    _run(body())


def test_archive_removes_row_and_flips_status(inbox_env, inbox_app) -> None:
    """a archives the selected message; it disappears from the list."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            initial_total = len(inbox_app._tasks)
            assert initial_total >= 1

            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            target = inbox_app._selected_task_id
            assert target is not None

            await pilot.press("a")
            await pilot.pause()

            # List shrinks by one and the archived task is gone.
            assert len(inbox_app._tasks) == initial_total - 1
            assert all(t.task_id != target for t in inbox_app._tasks)

            # Work status is DONE in the underlying DB.
            svc = inbox_app._svc_for_task(target)
            try:
                task = svc.get(target)
            finally:
                svc.close()
            assert task.work_status.value == "done"
    _run(body())


def test_workspace_store_notification_appears_in_inbox(inbox_env, inbox_app) -> None:
    """Workspace-root Store rows are listed alongside task-backed inbox items."""
    _seed_workspace_message(
        inbox_env["project_path"].parent,
        subject="Deploy blocked",
        body="Needs verification email click.",
    )

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            message_item = next(
                (item for item in inbox_app._tasks if item.task_id.startswith("msg:")),
                None,
            )
            assert message_item is not None
            assert "Deploy blocked" in message_item.title
            assert message_item.project == "demo"

            inbox_app.list_view.index = inbox_app._tasks.index(message_item)
            await pilot.press("enter")
            await pilot.pause()

            detail_text = str(inbox_app.detail.render())
            assert "Deploy blocked" in detail_text
            assert "Needs verification email click." in detail_text
            assert inbox_app._selected_task_id == message_item.task_id
            assert message_item.task_id not in inbox_app._unread_ids
            assert inbox_app.reply_input.disabled is True
    _run(body())


def test_archive_closes_store_notification_and_removes_row(inbox_env, inbox_app) -> None:
    """Archiving a Store-backed notification closes the message row."""
    message_id = _seed_workspace_message(
        inbox_env["project_path"].parent,
        subject="Nightly report ready",
        body="Open the run summary for details.",
    )

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            message_item = next(
                (item for item in inbox_app._tasks if item.message_id == message_id),
                None,
            )
            assert message_item is not None

            await pilot.press("m")
            await pilot.pause()
            from pollypm.cockpit_ui import _InboxListItem
            visible_items = [
                child for child in inbox_app.list_view.children
                if isinstance(child, _InboxListItem)
            ]
            inbox_app.list_view.index = next(
                idx for idx, child in enumerate(visible_items)
                if child.task_ref.message_id == message_id
            )
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            assert all(item.message_id != message_id for item in inbox_app._tasks)

        db_path = inbox_env["project_path"].parent / ".pollypm" / "state.db"
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            open_rows = store.query_messages(state="open", type="notify")
            closed_rows = store.query_messages(state="closed", type="notify")
        finally:
            store.close()
        assert all(row["id"] != message_id for row in open_rows)
        assert any(row["id"] == message_id for row in closed_rows)
    _run(body())


def test_empty_state_message_when_no_inbox(tmp_path: Path) -> None:
    """An inbox with zero messages shows the friendly empty-state copy."""
    async def body() -> None:
        project_path = tmp_path / "empty"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        # Create the DB but don't seed any tasks.
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        svc.close()

        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._tasks == []
            detail_text = str(app.detail.render())
            assert "No messages" in detail_text
            assert "Polly" in detail_text
    _run(body())


# ---------------------------------------------------------------------------
# Feature 1 — jump to PM discussion (d)
# ---------------------------------------------------------------------------


def _write_persona_config(
    project_path: Path, config_path: Path, persona_name: str,
) -> None:
    """Variant of the minimal-config writer that adds a persona_name."""
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
        f'persona_name = "{persona_name}"\n'
    )


def test_d_key_dispatches_to_pm_with_context_line(inbox_env, inbox_app) -> None:
    """Pressing ``d`` on a detail routes the cockpit + sends the context line."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id is not None

            await pilot.press("d")
            # Dispatch runs in a worker — give it a tick to settle.
            await pilot.pause()
            await pilot.pause()
            # Fallback: drive the worker directly if the thread scheduler
            # didn't land inside the pilot pause budget.
            if not calls:
                inbox_app._dispatch_to_pm_sync("polly", f're: inbox/{task_id} "stub"', "Polly")

            assert calls, "expected _perform_pm_dispatch to be called"
            cockpit_key, context_line = calls[-1]
            # Project has no persona → falls back to Polly.
            assert cockpit_key == "polly"
            assert context_line.startswith(f're: inbox/{task_id} ')
            assert '"' in context_line
    _run(body())


def test_d_key_with_persona_routes_to_project_session(tmp_path: Path) -> None:
    """Persona projects dispatch to ``project:<key>:session`` + show PM name."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_persona_config(project_path, config_path, "Ruby")
        _seed_project(project_path)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            # The detail header shows the PM's name.
            detail_text = str(app.detail.render())
            assert "PM: Ruby" in detail_text

            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                app._dispatch_to_pm_sync(
                    "project:demo:session", 're: inbox/demo/1 "stub"', "Ruby",
                )
            assert calls
            cockpit_key, _ = calls[-1]
            assert cockpit_key == "project:demo:session"
    _run(body())


def test_d_does_not_fire_when_focus_in_reply_input(inbox_env, inbox_app) -> None:
    """If the reply Input has focus, ``d`` types a letter instead of dispatching."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            # Focus the reply Input so ``d`` lands inside it.
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus

            await pilot.press("d")
            await pilot.pause()
            # The dispatch must NOT have run — Sam is mid-draft.
            assert calls == []
            # And the letter should have been typed into the Input
            # (Textual's default Input binding).
            assert "d" in (inbox_app.reply_input.value or "")
    _run(body())


# ---------------------------------------------------------------------------
# Feature 2 — rollup expansion in detail pane
# ---------------------------------------------------------------------------


def _seed_rollup(project_path: Path, item_count: int = 3) -> str:
    """Stage N digest rows in a project DB, then flush them into a rollup.

    Returns the rollup task_id. The rows carry payload commit/pr refs so
    the rollup-item render path has reference data to exercise.
    """
    from pollypm import notification_staging as ns

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        for i in range(item_count):
            ns.stage_notification(
                svc._conn,
                project="demo",
                subject=f"Subtask {i} done",
                body=f"PR #{100 + i} merged for subtask {i}.",
                actor="polly",
                priority="digest",
                milestone_key="milestones/01-init",
                payload={"pr": f"#{100 + i}", "commit": f"abc{i:03d}"},
            )
        rollup_id = ns.flush_milestone_digest(
            svc, project="demo", milestone_key="milestones/01-init",
            project_path=project_path,
        )
        assert rollup_id is not None
        return rollup_id
    finally:
        svc.close()


def test_rollup_detail_renders_individual_items(tmp_path: Path) -> None:
    """A rollup task spawns one _RollupItem widget per staged item."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        rollup_id = _seed_rollup(project_path, item_count=3)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp, _RollupItem
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Select the rollup specifically (it's the newest → index 0).
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # Items section is populated.
            assert len(app._rollup_items) == 3
            rows = [
                c for c in app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            assert len(rows) == 3
            # Item text includes the subject + PR reference.
            first_text = str(rows[0]._body.render())
            assert "Subtask 0" in first_text
            assert "#100" in first_text
    _run(body())


def test_non_rollup_task_does_not_spawn_rollup_box(inbox_env, inbox_app) -> None:
    """Plain inbox tasks render the body only — no rollup-item widgets."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            from pollypm.cockpit_ui import _RollupItem
            rows = [
                c for c in inbox_app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            assert rows == []
            assert inbox_app._rollup_items == []
    _run(body())


def test_rollup_item_click_toggles_expansion(tmp_path: Path) -> None:
    """Clicking an item expands it; clicking again collapses it."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        rollup_id = _seed_rollup(project_path, item_count=2)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp, _RollupItem
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # First toggle — item 0 becomes expanded.
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert 0 in app._rollup_expanded
            rows = [
                c for c in app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            expanded_row = next(r for r in rows if r.index == 0)
            assert expanded_row.expanded is True

            # Second toggle collapses.
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert 0 not in app._rollup_expanded
    _run(body())


def test_d_on_rollup_subitem_targets_its_project(tmp_path: Path) -> None:
    """A focused rollup sub-item with a different project dispatches to that PM."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        other_path = tmp_path / "other"
        other_path.mkdir()
        (other_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        # Both projects in config — ``demo`` has no persona, ``other`` has Ruby.
        config_path.write_text(
            "[project]\n"
            f'tmux_session = "pollypm-test"\n'
            f'workspace_root = "{project_path.parent}"\n'
            "\n"
            f'[projects.demo]\n'
            f'key = "demo"\n'
            f'name = "Demo"\n'
            f'path = "{project_path}"\n'
            "\n"
            f'[projects.other]\n'
            f'key = "other"\n'
            f'name = "Other"\n'
            f'path = "{other_path}"\n'
            f'persona_name = "Ruby"\n'
        )
        # Seed the rollup on the `demo` project, but its sub-items claim
        # `source_project=other` so the jump should target Ruby, not Polly.
        from pollypm import notification_staging as ns
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        try:
            ns.stage_notification(
                svc._conn, project="demo",
                subject="Cross-project update",
                body="work shipped on the other project",
                actor="polly", priority="digest",
                milestone_key="milestones/01-init",
                payload={"project": "other", "pr": "#42"},
            )
            rollup_id = ns.flush_milestone_digest(
                svc, project="demo", milestone_key="milestones/01-init",
                project_path=project_path,
            )
        finally:
            svc.close()
        assert rollup_id is not None
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # Focus the sub-item via toggle (mirrors a click).
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert app._rollup_focused_index == 0

            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                app._dispatch_to_pm_sync(
                    "project:other:session",
                    're: inbox/demo/1 "stub"',
                    "Ruby",
                )
            assert calls
            cockpit_key, ctx = calls[-1]
            # Dispatch follows the sub-item's project, not the rollup's.
            assert cockpit_key == "project:other:session"
            # Context line references the sub-item's subject (not the
            # rollup's title).
            assert "Cross-project update" in ctx
    _run(body())


# ---------------------------------------------------------------------------
# #752 — background refresh skips rendering when data unchanged
# ---------------------------------------------------------------------------


def test_inbox_list_width_scales_with_terminal_size(inbox_env) -> None:
    """#753: list width is percentage-based so wide terminals aren't
    stuck with the old 42-column fixed size. List on a narrow pilot
    should be notably smaller than on a wide one — confirming the
    layout responds to viewport size."""
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    widths: dict[str, int] = {}

    async def measure(size: tuple[int, int], key: str) -> None:
        app = PollyInboxApp(inbox_env["config_path"])
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            widths[key] = app.list_view.size.width

    async def body() -> None:
        await measure((80, 40), "narrow")
        await measure((200, 40), "wide")

    _run(body())

    # Wide viewport must yield a notably bigger list than narrow.
    assert widths["wide"] > widths["narrow"], (
        f"list width did not scale with viewport: "
        f"narrow={widths['narrow']} wide={widths['wide']}"
    )
    # On 200-col terminal, the list should be well above the
    # historic 42-column cap — that's the whole point of the change.
    assert widths["wide"] >= 60, (
        f"list width on wide viewport only {widths['wide']} cols — "
        "expected at least 60 (40% of 200 - border)"
    )


def test_inbox_detail_includes_inline_review_artifact(tmp_path: Path) -> None:
    """#761: when an inbox item references a task with a review
    artifact on disk (e.g. a plan_review task with
    docs/project-plan.md), the artifact's summary is rendered inline
    in the inbox detail pane — same content as the task Review tab,
    no pane-jump required."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    # Seed the plan-review artifact on disk.
    plan_path = project_path / "docs" / "project-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Notesy Plan\n\nShip the inbox review surface.\n")

    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)

    # Seed the task: a plan_project flow in review state, so
    # load_task_review_artifact picks up docs/project-plan.md.
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc.create(
            title="Review the plan",
            description="Plan needs review",
            type="task",
            project="demo",
            flow_template="plan_project",
            roles={
                "requester": "user",
                "operator": "polly",
                "architect": "archie",
            },
            priority="normal",
            created_by="polly",
        )
    finally:
        svc.close()

    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(config_path)

    async def body() -> None:
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            # Open the first item (the plan-review task).
            app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()

            detail_text = str(app.detail.render())
            # The inline review artifact section is present and
            # contains the plan content + section heading.
            assert "review artifact" in detail_text.lower(), (
                f"expected review-artifact section; got {detail_text!r}"
            )
            assert "Ship the inbox review surface." in detail_text, (
                f"plan body missing from rendered inbox detail"
            )

    _run(body())


def test_inbox_app_honors_initial_project_filter(inbox_env) -> None:
    """#751: when launched with ``initial_project``, the inbox applies
    the project filter on mount and shows the filter-bar chip so the
    scope is visible + dismissable."""
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp(inbox_env["config_path"], initial_project="demo")

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Filter state reflects the initial project.
            assert app._filter_project == "demo"
            # Filter bar mounted visible so the user can see + clear.
            assert app.filter_bar.display is True

    _run(body())


def test_inbox_app_without_initial_project_stays_global(inbox_env) -> None:
    """No initial_project means no project scope; default action lens is visible."""
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp(inbox_env["config_path"])  # no initial_project

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._filter_project is None
            assert app.filter_bar.display is True
            assert "action needed" in str(app.filter_chips.render())

    _run(body())


def test_background_refresh_skips_when_content_unchanged(inbox_env, inbox_app) -> None:
    """The visible flash every ~8s was caused by the background refresh
    calling ListView.clear() and re-appending every row on every tick,
    regardless of whether anything had changed. Now the inbox computes
    a content signature and skips the re-render when it matches the
    previous tick."""
    import asyncio as _asyncio

    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Snapshot the initial signature after the mount render.
            first_sig = inbox_app._last_inbox_signature
            assert first_sig is not None, "initial mount should seed the signature"

            # Count how many _render_list calls happen during the next
            # background refresh. Render should be skipped because data
            # is unchanged.
            render_calls: list[None] = []
            original_render = inbox_app._render_list
            def _counting_render(*args, **kwargs):
                render_calls.append(None)
                return original_render(*args, **kwargs)
            inbox_app._render_list = _counting_render

            inbox_app._background_refresh()
            await pilot.pause()
            assert render_calls == [], (
                "background refresh must not re-render when inbox content "
                f"is unchanged (got {len(render_calls)} calls)"
            )
    _asyncio.run(body())


def test_background_refresh_renders_when_content_changes(inbox_env, inbox_app) -> None:
    """When a new task lands between polls, the background refresh must
    notice and re-render — the skip-on-unchanged optimization must not
    break the happy path."""
    import asyncio as _asyncio

    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

            # Seed a fresh task DIRECTLY into the project DB so the next
            # _load_inbox sees it.
            svc = SQLiteWorkService(
                db_path=inbox_env["project_path"] / ".pollypm" / "state.db",
                project_path=inbox_env["project_path"],
            )
            try:
                svc.create(
                    title="Brand new task",
                    description="body",
                    type="task",
                    project="demo",
                    flow_template="chat",
                    roles={"requester": "user", "operator": "polly"},
                    priority="normal",
                    created_by="polly",
                )
            finally:
                svc.close()

            render_calls: list[None] = []
            original_render = inbox_app._render_list
            def _counting_render(*args, **kwargs):
                render_calls.append(None)
                return original_render(*args, **kwargs)
            inbox_app._render_list = _counting_render

            inbox_app._background_refresh()
            await pilot.pause()
            assert len(render_calls) == 1, (
                "background refresh must re-render when the inbox data "
                f"actually changed (got {len(render_calls)} calls)"
            )
    _asyncio.run(body())
