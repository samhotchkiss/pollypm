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

from types import SimpleNamespace

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


def test_actionable_alert_filter_drops_stale_watchdog_queue_alerts() -> None:
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset({"media", "polly_remote"}),
        tracked_projects=frozenset({"polly_remote"}),
        project_task_counts={
            "media": {"queued": 4},
            "polly_remote": {"queued": 0},
        },
    )

    untracked_alert = SimpleNamespace(
        session_name="audit-queue_without_motion-media-media",
        alert_type="audit_watchdog",
        message="Project media has 4 queued task(s) but no motion.",
    )
    resolved_alert = SimpleNamespace(
        session_name="audit-queue_without_motion-polly_remote-polly_remote",
        alert_type="audit_watchdog",
        message="Project polly_remote has 2 queued task(s) but no motion.",
    )
    live_alert = SimpleNamespace(
        session_name="audit-queue_without_motion-polly_remote-polly_remote",
        alert_type="audit_watchdog",
        message="Project polly_remote has 2 queued task(s) but no motion.",
    )

    assert not is_user_actionable_alert(untracked_alert, context=context)
    assert not is_user_actionable_alert(resolved_alert, context=context)
    live_context = AlertActionabilityContext(
        known_projects=context.known_projects,
        tracked_projects=context.tracked_projects,
        project_task_counts={"polly_remote": {"queued": 2}},
    )
    assert is_user_actionable_alert(live_alert, context=live_context)


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


def test_briefing_pluralizes_counts_correctly() -> None:
    """The Home briefing stays readable for singular and plural counts."""
    from pollypm.dashboard_data import (
        _build_dashboard_briefing,
        CommitInfo,
        CompletedItem,
        InboxPreview,
    )

    # Singular case — no parenthetical-s.
    out = _build_dashboard_briefing(
        commits=[CommitInfo("h1", "msg", "a", 0.0, "demo")],
        completed=[CompletedItem("t", "issue", "demo", 0.0)],
        inbox_count=1,
        recent_messages=[
            InboxPreview(
                sender="polly",
                title="Approval ready: demo/1",
                project="demo",
                task_id="demo/1",
                age_seconds=0.0,
            )
        ],
        recovery_count_24h=1,
    )
    assert "1 commit across 1 project" in out
    assert "1 item wrapped" in out
    assert "1 recovery handled" in out
    assert "One thing needs you:" in out
    assert "demo/1" not in out
    assert out.startswith("Morning."), f"unexpected greeting: {out!r}"
    # The bare singular forms must not contain the legacy parens.
    assert "(s)" not in out
    assert "(ies)" not in out

    # Plural case — proper plural endings, still no parens.
    out2 = _build_dashboard_briefing(
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
        recent_messages=[
            InboxPreview(
                sender="polly",
                title="Decision ready for other/99",
                project="other",
                task_id="other/99",
                age_seconds=0.0,
            )
        ],
        recovery_count_24h=2,
    )
    assert "3 commits across 2 projects" in out2
    assert "2 items wrapped" in out2
    assert "2 recoveries handled" in out2
    assert "First up:" in out2
    assert "4 inbox items waiting" in out2
    assert "other/99" not in out2
    assert "(s)" not in out2
    assert "(ies)" not in out2


def test_briefing_all_handled_when_no_activity() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=0,
        recent_messages=[],
        recovery_count_24h=0,
    )

    assert "Quiet night" in out
    assert "Nothing needs you" in out


# ---------------------------------------------------------------------------
# #2308 perf — _session_description cache contract.
#
# The dashboard handler invokes ``_session_description`` once per active
# session per request. On a 16-project / 44-session workspace that's 44
# tmux-snapshot reads × ~10ms each per request, and the snapshot only
# rolls forward when the heartbeat sweep (~30s cadence) rewrites it.
# Caching by ``(snapshot_path, mtime_ns, status, role)`` collapses
# repeat calls into one parse per (file, version, view-key) tuple.
# These two tests pin the cache contract: a key match reuses the prior
# result, and an mtime bump forces a fresh parse.
# ---------------------------------------------------------------------------


def test_session_description_cache_returns_cached_when_key_matches(
    tmp_path, monkeypatch
) -> None:
    """Repeat ``_session_description`` calls with the same
    ``(snapshot_path, mtime_ns, status, role)`` must reuse the cached
    parse — no second call into ``_compute_session_description``.
    """
    from pollypm import dashboard_data
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("⏺ Working (1m 2s · 4.2k tokens · esc to interrupt)\n")

    # Clear the module-level cache so prior tests don't preload our key.
    dashboard_data._SESSION_DESCRIPTION_CACHE.clear()

    calls: list[tuple[str, str, str | None]] = []
    real_compute = dashboard_data._compute_session_description

    def counting_compute(status: str, role: str, snapshot_path: str | None) -> str:
        calls.append((status, role, snapshot_path))
        return real_compute(status, role, snapshot_path)

    monkeypatch.setattr(
        dashboard_data, "_compute_session_description", counting_compute
    )

    first = _session_description("healthy", "worker", str(snapshot))
    second = _session_description("healthy", "worker", str(snapshot))
    third = _session_description("healthy", "worker", str(snapshot))

    # All callers see the same description and the heavy parser ran once.
    assert first == second == third
    assert len(calls) == 1, (
        f"expected one compute call, got {len(calls)}: {calls}"
    )


def test_session_description_cache_refreshes_on_mtime_change(
    tmp_path, monkeypatch
) -> None:
    """When the snapshot file's mtime advances (heartbeat rewrote the
    pane), the cache key changes and ``_compute_session_description``
    must run again — even though the path + status + role are
    identical. This is what prevents the dashboard from serving a
    stale Now-feed line when the underlying session has moved on.
    """
    from pollypm import dashboard_data
    from pollypm.dashboard_data import _session_description

    snapshot = tmp_path / "snap.txt"
    snapshot.write_text("⏺ Working (1m 2s · 4.2k tokens · esc to interrupt)\n")

    dashboard_data._SESSION_DESCRIPTION_CACHE.clear()

    calls: list[tuple[str, str, str | None]] = []
    real_compute = dashboard_data._compute_session_description

    def counting_compute(status: str, role: str, snapshot_path: str | None) -> str:
        calls.append((status, role, snapshot_path))
        return real_compute(status, role, snapshot_path)

    monkeypatch.setattr(
        dashboard_data, "_compute_session_description", counting_compute
    )

    first = _session_description("healthy", "worker", str(snapshot))
    assert len(calls) == 1

    # Rewrite the snapshot with new content + bump mtime. We force the
    # mtime forward explicitly so this is robust on filesystems with
    # coarse mtime granularity (1s on some platforms).
    snapshot.write_text("⏺ Working (5m 30s · 9.0k tokens · esc to interrupt)\n")
    new_mtime_ns = snapshot.stat().st_mtime_ns + 1_000_000_000
    import os

    os.utime(snapshot, ns=(new_mtime_ns, new_mtime_ns))

    second = _session_description("healthy", "worker", str(snapshot))

    # mtime bump means a new cache key, so the parser ran again, and
    # the freshly-parsed result reflects the rewritten snapshot.
    assert len(calls) == 2, (
        f"expected two compute calls after mtime bump, got {len(calls)}: {calls}"
    )
    assert first != second
    assert "5m" in second


# ---------------------------------------------------------------------------
# Cycle 86 (sqlite-only) — REMOVED for Slice K (#1737).
#
# The three tests that previously lived here drove the per-project
# sqlite ``.pollypm/state.db`` fanout inside
# ``dashboard_data._count_inbox_tasks`` /
# ``_user_waiting_task_ids_across_projects`` / ``_recent_inbox_messages``.
# Under the pg backend that fanout is dead code — the bulk pg query in
# ``cockpit_pg_aggregates.inbox_tasks_grouped`` replaces it. Pg-side
# tracked-filter coverage lives in ``tests/test_dashboard_data_pg.py``.
# ---------------------------------------------------------------------------
