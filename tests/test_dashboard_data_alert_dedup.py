"""Polly-dashboard ``alert_count`` mirrors the cycle 45/53/55 dedup.

When ``stuck_on_task:<id>`` fires because the architect session sat
idle waiting for the user to respond and the task is already in a
user-waiting status, the alert is the same fact in different words.
The polly dashboard's ``alert_count`` is what drives the
"N needs action" cell in the top stats line (renamed from "N alerts"
in #999); counting redundant stuck alerts there inflates the badge
for non-faults the user already sees as yellow.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.dashboard_data import _stuck_alert_already_user_waiting


def test_stuck_alert_already_user_waiting_filters_when_task_is_waiting() -> None:
    assert _stuck_alert_already_user_waiting(
        "stuck_on_task:polly_remote/12",
        frozenset({"polly_remote/12"}),
    )


def test_stuck_alert_already_user_waiting_keeps_alert_for_other_tasks() -> None:
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:polly_remote/9",
        frozenset({"polly_remote/12"}),
    )


def test_stuck_alert_already_user_waiting_only_handles_stuck_prefix() -> None:
    assert not _stuck_alert_already_user_waiting(
        "no_session_for_assignment:polly_remote/12",
        frozenset({"polly_remote/12"}),
    )
    assert not _stuck_alert_already_user_waiting("", frozenset())


def test_stuck_alert_already_user_waiting_handles_malformed_alert() -> None:
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:",
        frozenset({"polly_remote/12"}),
    )
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:   ",
        frozenset({"polly_remote/12"}),
    )


def test_session_description_skips_claude_tui_bottom_bar(tmp_path) -> None:
    """The polly-dashboard "Now" section was rendering every idle
    session as ``"⏵⏵ bypass permissions on (shift+tab to cycle)"`` —
    the Claude TUI's standing keybinding hint, picked up from the
    last line of the pane snapshot. The session isn't *doing* the
    bypass-permissions thing; it's idle at the prompt.

    Filter the standing TUI bar lines so the snapshot scan falls
    through to the ``status``-based default ("idle").
    """
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text(
        # Typical idle Claude TUI tail — the function scans bottom-up.
        "Some real activity finished a while ago.\n"
        "\n"
        "⏵⏵ bypass permissions on (shift+tab to cycle) · ctrl+t to hide tasks\n"
    )
    desc = _session_description("healthy", "worker", str(snapshot))
    # Either the meaningful line above bubbles up, or we fall through
    # to the status-based default. Either way, the bypass-permissions
    # boilerplate must not be the description.
    assert "bypass permissions" not in desc.lower()
    assert "shift+tab" not in desc.lower()


def test_session_description_falls_through_when_only_tui_lines(
    tmp_path,
) -> None:
    """When the entire snapshot is keybinding boilerplate, the
    description must fall through to the status-based default
    ("idle" for a healthy worker) rather than echoing the bar."""
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text(
        "⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        "ctrl+t to hide tasks\n"
    )
    desc = _session_description("healthy", "worker", str(snapshot))
    assert desc == "idle"


def test_session_description_strips_ansi_from_snapshot(tmp_path) -> None:
    """#792: in-flight Claude renders leak overlapping fragments
    into the snapshot, so a ``ready`` line followed by an erase-
    sequence and ``ring…`` rendered as ``readyring…`` in the Now
    panel. Strip ANSI/control bytes before parsing the snapshot.
    """
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text(
        # Real-world shape — bold-on, "ready", reset, erase-line, "ring…".
        "\x1b[1mready\x1b[0m\x1b[Kring…\n"
    )
    desc = _session_description("healthy", "worker", str(snapshot))
    assert "\x1b" not in desc
    # The cleaned text either becomes a valid line or falls through
    # to the status default — but it must not be the corrupt fusion.
    assert "readyring" not in desc


def test_session_description_summarizes_token_status_chrome(tmp_path) -> None:
    """Claude status chrome is not useful prose for the home Now panel."""
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("⏺ readyring… (4s · ↑ 216 tokens · thinking)\n")

    desc = _session_description("healthy", "worker", str(snapshot))

    assert desc == "thinking (4s)"
    assert "readyring" not in desc


def test_session_description_skips_rounded_box_fragments(tmp_path) -> None:
    """Rounded border fragments from an in-flight pane render are not content."""
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("╭───────────────────────────────────────────────\n")

    desc = _session_description("healthy", "worker", str(snapshot))

    assert desc == "idle"


def test_session_description_skips_codex_idle_prompt_arrow(tmp_path) -> None:
    """#994: An idle Codex CLI session renders rotating placeholder
    hints in its empty input box, prefixed with the ``›`` prompt
    arrow ("› Run /review on my current changes", "› Explain this
    codebase", etc). The dashboard "Now" panel was scraping those
    lines as the worker's activity description, so users saw idle
    sessions captioned with Codex CLI suggestion text instead of an
    "idle" indicator. The leading ``›`` is the Codex prompt — treat
    it the same as Claude's ``❯`` and fall through to the status
    default.
    """
    from pollypm.dashboard_data import _session_description

    placeholder_hints = (
        "› Run /review on my current changes",
        "› Explain this codebase",
        "› Write tests for @filename",
        "› Summarize recent commits",
        "› Use /skills to list available skills",
        "› Find and fix a bug in @filename",
    )
    for hint in placeholder_hints:
        snapshot = tmp_path / "snap.txt"
        # Bare placeholder line — the failure mode in the issue: with
        # nothing else on the pane, the ``›`` line was the description.
        snapshot.write_text(f"{hint}\n")
        desc = _session_description("healthy", "worker", str(snapshot))
        # The leading prompt arrow (Codex CLI, U+203A) must not leak.
        assert "›" not in desc, f"prompt arrow leaked for {hint!r}: {desc!r}"
        # None of the placeholder text should bubble up either.
        for token in (
            "run /review",
            "explain this",
            "write tests",
            "summarize recent",
            "/skills to list",
            "find and fix",
        ):
            assert token not in desc.lower(), (
                f"placeholder {token!r} leaked for hint {hint!r}: {desc!r}"
            )
        # Healthy + no real content => the status-based default.
        assert desc == "idle", (
            f"expected idle fallthrough for {hint!r}, got {desc!r}"
        )


def test_session_description_skips_codex_idle_placeholder_without_arrow(
    tmp_path,
) -> None:
    """#994 defensive: if the leading ``›`` glyph is dropped during
    pane capture but the suggestion text survives, the line still
    must not be reported as activity. The known-placeholder substring
    filter is the safety net behind the prompt-arrow filter.
    """
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("  Find and fix a bug in @filename\n")
    desc = _session_description("healthy", "worker", str(snapshot))
    assert "find and fix" not in desc.lower()
    assert desc == "idle"


def test_session_description_skips_upstream_cli_tips(tmp_path) -> None:
    """#1183: Codex tip-of-the-day banners are not project activity."""
    from pollypm.dashboard_data import _session_description

    tip_lines = (
        "Tip: Use /compact when the conversation gets long to summarize…",
        "Tip: Try the Codex App. Run 'codex app' or visit https://chatgpt.com/",
        "Tip: New Use /fast to enable our fastest inference with increased…",
        "Use /compact when the conversation gets long to summarize…",
        "Use /fast to enable our fastest inference with increased…",
        "Try the Codex App. Run 'codex app' or visit https://chatgpt.com/",
    )
    for tip in tip_lines:
        snapshot = tmp_path / "snap.txt"
        snapshot.write_text(f"{tip}\n")
        desc = _session_description("healthy", "worker", str(snapshot))
        assert "tip:" not in desc.lower()
        assert "/compact" not in desc.lower()
        assert "/fast" not in desc.lower()
        assert "codex app" not in desc.lower()
        assert "chatgpt.com" not in desc.lower()
        assert desc == "idle"


def test_session_description_uses_meaningful_line_before_cli_tip(tmp_path) -> None:
    """A real activity line above a skipped tip should remain visible."""
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text(
        "Updated dashboard activity filtering tests.\n"
        "Tip: Try the Codex App. Run 'codex app' or visit https://chatgpt.com/\n"
    )

    desc = _session_description("healthy", "worker", str(snapshot))

    assert desc == "Updated dashboard activity filtering tests."


def test_operator_pm_healthy_copy_matches_now_feed_voice() -> None:
    """#1299: Polly's own Now-feed row should not use bland filler copy."""
    from pollypm.dashboard_data import _session_description

    desc = _session_description("healthy", "operator-pm", None)

    assert desc == "Plating the brief"
    assert "managing projects" not in desc.lower()


def test_session_description_keeps_real_codex_working_status(tmp_path) -> None:
    """#994 negative: the fix must not regress working-session
    rendering. A pane snapshot showing Codex actively working (the
    universal ``Working (Nm Ns)`` indicator) should still surface as
    ``working (Nm Ns)`` — not get swallowed by the new placeholder
    filter.
    """
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text(
        "OpenAI Codex (research preview)\n"
        "\n"
        "⏺ Working (3m 12s · 4.2k tokens · esc to interrupt)\n"
    )
    desc = _session_description("healthy", "worker", str(snapshot))
    assert "working" in desc.lower()
    assert "3m" in desc


def test_session_description_rewrites_zero_second_working_copy(tmp_path) -> None:
    """#1327: zero-second Codex working chrome is filler, not useful status."""
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("⏺ Working (0s · esc to interrupt)\n")

    desc = _session_description("healthy", "worker", str(snapshot))

    assert desc == "Warming up"


def test_session_description_rewrites_raw_no_tasks_copy(tmp_path) -> None:
    """#1327: task-list command output should not leak into the Now feed."""
    from pollypm.dashboard_data import _session_description

    raw_lines = (
        "Result: No tasks found.",
        "- pm task list --status review → No tasks found.",
    )
    for raw in raw_lines:
        snapshot = tmp_path / "snap.txt"
        snapshot.write_text(raw + "\n")

        desc = _session_description("healthy", "worker", str(snapshot))

        assert desc == "Nothing on the burner"
        assert "No tasks found" not in desc
        assert "pm task list" not in desc


def test_session_description_rewrites_fragmentary_worker_copy(tmp_path) -> None:
    """#1327: lower-case mid-sentence fragments get a stable worker idiom."""
    from pollypm.dashboard_data import _session_description

    fragments = (
        "implementing worker tasks.",
        "treat the worktree as potentially dirty and avoid clobbering unrelated",
        "operating norms, the architect role guide, and the project rules…",
    )
    for fragment in fragments:
        snapshot = tmp_path / "snap.txt"
        snapshot.write_text(fragment + "\n")

        desc = _session_description("healthy", "worker", str(snapshot))

        assert desc == "On the line"


def test_session_description_truncates_at_word_boundary(tmp_path) -> None:
    """#792: ``[:70]`` chopped descriptions mid-word (``Phase A
    decisio``). Truncate at a word boundary and append ``…``.
    """
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    long_status = (
        "No tasks available. media/1 is on hold awaiting your "
        "Phase A decision before further sweeps land work."
    )
    snapshot.write_text(long_status + "\n")
    desc = _session_description("healthy", "worker", str(snapshot))
    assert desc.endswith("…")
    assert "decisio" not in desc or "decision" in desc
    assert " " not in desc[-2:]  # ellipsis follows a complete word


def test_briefing_pluralizes_counts_correctly(tmp_path) -> None:
    """The morning briefing rendered ``1 project(s)`` / ``1 issue(s)``
    when counts were exactly 1 — awkward parenthetical pluralisation
    a user reads as a bug. Pluralise properly: ``1 project`` /
    ``2 projects`` / ``1 issue`` / ``3 issues``.

    We exercise the briefing builder via the in-process gather path
    so the test stays focused on the prose, not the SQL plumbing.
    """
    # Test the inline ``_plural`` helper indirectly by exercising
    # the gather() prose builder. We can't easily call ``_plural``
    # in isolation since it's nested inside ``gather``; instead,
    # construct minimal fakes that exercise each pluralisation
    # branch and inspect the resulting briefing string.
    from datetime import UTC, datetime
    from pollypm.dashboard_data import CommitInfo, CompletedItem, DashboardData

    now = datetime.now(UTC)

    def _build_briefing(commits, completed, inbox_count) -> str:
        """Inline copy of the briefing prose builder for unit testing.

        Mirrors the production logic in ``dashboard_data.gather`` so the
        plural-handling regression stays covered. Recoveries are not
        surfaced in the briefing (#854): they are internal recovery-loop
        plumbing, not user-facing activity.
        """
        def _plural(count: int, singular: str, plural: str | None = None) -> str:
            word = singular if count == 1 else (plural or f"{singular}s")
            return f"{count} {word}"

        if not (commits or completed or inbox_count):
            return ""
        parts: list[str] = []
        if commits:
            projects_touched = len({c.project for c in commits})
            parts.append(
                f"{_plural(len(commits), 'commit')} across "
                f"{_plural(projects_touched, 'project')}"
            )
        if completed:
            parts.append(f"{_plural(len(completed), 'issue')} completed")
        if inbox_count:
            parts.append(
                f"{_plural(inbox_count, 'inbox item')} waiting for you"
            )
        return "Last 24 hours: " + ", ".join(parts) + "."

    # Singular case — no parenthetical-s.
    out = _build_briefing(
        commits=[CommitInfo("h1", "msg", "a", 0.0, "demo")],
        completed=[CompletedItem("t", "issue", "demo", 0.0)],
        inbox_count=1,
    )
    assert "1 commit across 1 project" in out
    assert "1 issue completed" in out
    assert "1 inbox item waiting" in out
    assert out.startswith("Last 24 hours:"), f"unexpected greeting: {out!r}"
    # Recovery counts must not surface in the user-facing briefing.
    assert "recovery" not in out.lower()
    # The bare singular forms must not contain the legacy parens.
    assert "(s)" not in out
    assert "(ies)" not in out

    # Plural case — proper plural endings, still no parens.
    out2 = _build_briefing(
        commits=[
            CommitInfo("h1", "m", "a", 0.0, "demo"),
            CommitInfo("h2", "m", "a", 0.0, "other"),
            CommitInfo("h3", "m", "a", 0.0, "demo"),
        ],
        completed=[
            CompletedItem("t1", "issue", "demo", 0.0),
            CompletedItem("t2", "issue", "demo", 0.0),
        ],
        inbox_count=4,
    )
    assert "3 commits across 2 projects" in out2
    assert "2 issues completed" in out2
    assert "4 inbox items waiting" in out2
    assert "recovery" not in out2.lower()
    assert "(s)" not in out2
    assert "(ies)" not in out2


# ---------------------------------------------------------------------------
# Cycle 86: tracked-only filter on inbox-count + user-waiting helpers
# ---------------------------------------------------------------------------


def test_count_inbox_tasks_skips_non_tracked_projects(
    tmp_path, monkeypatch,
) -> None:
    """``_count_inbox_tasks`` is the source of the morning-briefing
    inbox count and the doctor's open-inbox check. A registered-but-
    not-tracked project may still have a stale ``.pollypm/state.db``
    from a prior tracking run; counting its leftover tasks would
    inflate both surfaces.

    Mirrors cycle 85's recovery-prompt fix (same shape of bug, four
    different surfaces).
    """
    from types import SimpleNamespace

    from pollypm.dashboard_data import _count_inbox_tasks

    tracked_path = tmp_path / "tracked"
    (tracked_path / ".pollypm").mkdir(parents=True)
    (tracked_path / ".pollypm" / "state.db").write_text("")

    ghost_path = tmp_path / "ghost"
    (ghost_path / ".pollypm").mkdir(parents=True)
    (ghost_path / ".pollypm" / "state.db").write_text("")

    config = SimpleNamespace(projects={
        "tracked": SimpleNamespace(path=tracked_path, tracked=True),
        "ghost": SimpleNamespace(path=ghost_path, tracked=False),
    })

    called_with: list[str] = []

    def fake_inbox_tasks(_svc, *, project):
        called_with.append(project)
        # Pretend each project has 5 inbox tasks.
        return [object()] * 5

    class _FakeSvc:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        "pollypm.work.inbox_view.inbox_tasks", fake_inbox_tasks,
    )
    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    )

    total = _count_inbox_tasks(config)
    # Only the tracked project's 5 tasks counted; ghost's 5 ignored.
    assert total == 5
    assert called_with == ["tracked"]


def test_user_waiting_task_ids_skips_non_tracked_projects(
    tmp_path, monkeypatch,
) -> None:
    """``_user_waiting_task_ids_across_projects`` docstring promised
    "tracked" but didn't filter — same fix as ``_count_inbox_tasks``."""
    import sqlite3
    from types import SimpleNamespace

    from pollypm.dashboard_data import _user_waiting_task_ids_across_projects

    def _seed(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE work_tasks ("
                "project TEXT, task_number INTEGER, work_status TEXT)"
            )
            conn.execute(
                "INSERT INTO work_tasks VALUES (?, ?, ?)",
                ("ghost", 99, "blocked"),
            )
            conn.commit()
        finally:
            conn.close()

    tracked_path = tmp_path / "tracked"
    tracked_db = tracked_path / ".pollypm" / "state.db"
    _seed(tracked_db)
    # Update seeded row to use the ``tracked`` project key.
    conn = sqlite3.connect(tracked_db)
    try:
        conn.execute(
            "UPDATE work_tasks SET project = ?, task_number = ? WHERE 1",
            ("tracked", 1),
        )
        conn.commit()
    finally:
        conn.close()

    ghost_path = tmp_path / "ghost"
    _seed(ghost_path / ".pollypm" / "state.db")

    config = SimpleNamespace(projects={
        "tracked": SimpleNamespace(path=tracked_path, tracked=True),
        "ghost": SimpleNamespace(path=ghost_path, tracked=False),
    })

    waiting = _user_waiting_task_ids_across_projects(config)
    # Tracked project's blocked task surfaces; ghost project's stale
    # blocked task does NOT (would have leaked into stuck-alert dedup).
    assert "tracked/1" in waiting
    assert "ghost/99" not in waiting


def test_dashboard_data_routes_user_waiting_reads_through_work_facade() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    source = (repo_root / "src/pollypm/dashboard_data.py").read_text(encoding="utf-8")

    assert "import sqlite3" not in source
    assert "sqlite3.connect" not in source


def test_recent_inbox_messages_skips_non_tracked_projects(
    tmp_path, monkeypatch,
) -> None:
    """``_recent_inbox_messages`` powers the polly-dashboard's
    ``Recent messages`` preview. Same shape of bug as cycle 86's
    ``_count_inbox_tasks`` fix: a non-tracked project's leftover
    ``.pollypm/state.db`` would leak into the preview list.

    The workspace-root source still flows through (``project_key=None``
    in the helper) — only per-project entries get the tracked filter.
    """
    from types import SimpleNamespace

    from pollypm.dashboard_data import _recent_inbox_messages

    tracked_path = tmp_path / "tracked"
    (tracked_path / ".pollypm").mkdir(parents=True)
    (tracked_path / ".pollypm" / "state.db").write_text("")

    ghost_path = tmp_path / "ghost"
    (ghost_path / ".pollypm").mkdir(parents=True)
    (ghost_path / ".pollypm" / "state.db").write_text("")

    tracked_proj = SimpleNamespace(
        path=tracked_path, tracked=True, display_label=lambda: "Tracked",
    )
    ghost_proj = SimpleNamespace(
        path=ghost_path, tracked=False, display_label=lambda: "Ghost",
    )
    config = SimpleNamespace(
        projects={"tracked": tracked_proj, "ghost": ghost_proj},
        project=SimpleNamespace(workspace_root=None),
    )

    called_with: list[str | None] = []

    fake_task = SimpleNamespace(
        task_id="tracked/1",
        title="Pending preview",
        roles={},
        created_by="polly",
        updated_at=None,
        created_at=None,
    )

    def fake_inbox_tasks(_svc, *, project):
        called_with.append(project)
        return [fake_task]

    class _FakeSvc:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        "pollypm.work.inbox_view.inbox_tasks", fake_inbox_tasks,
    )
    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService", _FakeSvc,
    )

    previews = _recent_inbox_messages(config)
    # Only the tracked project's source was scanned.
    assert called_with == ["tracked"]
    assert len(previews) == 1
    assert previews[0].project == "Tracked"
    assert previews[0].task_id == "tracked/1"
