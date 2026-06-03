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

from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def test_actionable_count_excludes_recovery_warns_on_untracked_projects() -> None:
    """#2475 — the operator-Home "N things need you" headline must count
    only operator-actionable items. System-internal recovery watchdog
    warns (``plan_missing`` / ``worker_session_gap`` /
    ``missing_task_worker``) firing on stale / archived / synthetic test
    projects are recovery signals the heartbeat cascade auto-handles, so
    they're excluded from the count. The same warns on a tracked project
    stay actionable, and a genuine operator-decision alert is never
    demoted.
    """
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        count_user_actionable_alerts,
        is_user_actionable_alert,
        user_actionable_alerts,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset(
            {"polly_remote", "pm_test_alpha", "ghost", "queuestorm-beta"}
        ),
        tracked_projects=frozenset({"polly_remote"}),
    )

    # Operator-actionable: a genuine decision alert on a tracked project.
    operator_decision = SimpleNamespace(
        session_name="architect-polly_remote",
        alert_type="worker_question",
        message="Worker needs a scope call on polly_remote/14.",
    )
    # Operator-actionable: recovery warn, but on a TRACKED project — the
    # operator may legitimately want to claim its queued work.
    tracked_gap = SimpleNamespace(
        session_name="worker_session_gap-polly_remote",
        alert_type="worker_session_gap",
        message="Project polly_remote has 3 queued tasks but no workers.",
    )

    # System-internal recovery noise on stale / test projects — excluded.
    stale_plan_missing = SimpleNamespace(
        session_name="plan_gate-pm_test_alpha",
        alert_type="plan_missing",
        message="Project pm_test_alpha has queued tasks but no plan.",
    )
    stale_worker_gap = SimpleNamespace(
        session_name="worker_session_gap-ghost",
        alert_type="worker_session_gap",
        message="Project ghost has 2 queued tasks but no workers.",
    )
    stale_missing_worker = SimpleNamespace(
        session_name="missing_task_worker-queuestorm-beta/7",
        alert_type="missing_task_worker",
        message="Task queuestorm-beta/7 is in_progress but its worker died.",
    )

    alerts = [
        operator_decision,
        tracked_gap,
        stale_plan_missing,
        stale_worker_gap,
        stale_missing_worker,
    ]

    # Only the two tracked/operator-actionable items survive the filter.
    assert is_user_actionable_alert(operator_decision, context=context)
    assert is_user_actionable_alert(tracked_gap, context=context)
    assert not is_user_actionable_alert(stale_plan_missing, context=context)
    assert not is_user_actionable_alert(stale_worker_gap, context=context)
    assert not is_user_actionable_alert(stale_missing_worker, context=context)

    surviving = user_actionable_alerts(alerts, context=context)
    assert surviving == [operator_decision, tracked_gap]
    assert count_user_actionable_alerts(alerts, context=context) == 2


def test_recovery_warn_stays_actionable_without_tracked_set() -> None:
    """#2475 guard: with no ``tracked_projects`` set we can't tell stale
    from live, so a recovery warn is left actionable rather than
    over-suppressed (fail-open, not fail-closed)."""
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    warn = SimpleNamespace(
        session_name="plan_gate-ghost",
        alert_type="plan_missing",
        message="Project ghost has queued tasks but no plan.",
    )
    # No tracked_projects provided.
    assert is_user_actionable_alert(warn, context=AlertActionabilityContext())
    assert is_user_actionable_alert(warn)


def test_recovery_warn_demotes_tracked_but_dormant_project() -> None:
    """#2480 — trackedness is not a liveness signal.

    A dead project can remain ``tracked=True`` while watchdog churn keeps
    touching its tasks. Recovery/hygiene warns on such projects should
    not inflate the operator "needs you" headline unless the project has
    recent real completed work or real stalled work.
    """
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset({"polly_remote", "savethenovel"}),
        tracked_projects=frozenset({"polly_remote", "savethenovel"}),
        recent_real_work_projects=frozenset({"savethenovel"}),
    )

    dormant_recovery_warn = SimpleNamespace(
        session_name="worker_session_gap-polly_remote",
        alert_type="worker_session_gap",
        message="Project polly_remote has 3 queued tasks but no workers.",
    )
    active_recovery_warn = SimpleNamespace(
        session_name="worker_session_gap-savethenovel",
        alert_type="worker_session_gap",
        message="Project savethenovel has 2 queued tasks but no workers.",
    )
    operator_decision = SimpleNamespace(
        session_name="architect-polly_remote",
        alert_type="worker_question",
        message="Worker needs a scope call.",
    )

    assert not is_user_actionable_alert(dormant_recovery_warn, context=context)
    assert is_user_actionable_alert(active_recovery_warn, context=context)
    assert is_user_actionable_alert(operator_decision, context=context)


def test_recovery_warn_surfaces_tracked_project_with_stalled_work() -> None:
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset({"media", "pm_test_alpha"}),
        tracked_projects=frozenset({"media", "pm_test_alpha"}),
        recent_real_work_projects=frozenset(),
        project_task_counts={
            "media": {"queued": 2},
            "pm_test_alpha": {"queued": 4},
        },
    )
    media_plan_missing = SimpleNamespace(
        session_name="plan_gate-media",
        alert_type="plan_missing",
        message="Project media has queued tasks but no plan.",
    )
    fixture_plan_missing = SimpleNamespace(
        session_name="plan_gate-pm_test_alpha",
        alert_type="plan_missing",
        message="Project pm_test_alpha has queued tasks but no plan.",
    )

    assert is_user_actionable_alert(media_plan_missing, context=context)
    assert not is_user_actionable_alert(fixture_plan_missing, context=context)


def test_queue_without_motion_demotes_tracked_project_without_recent_real_work() -> None:
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset({"polly_remote"}),
        tracked_projects=frozenset({"polly_remote"}),
        recent_real_work_projects=frozenset(),
        project_task_counts={"polly_remote": {"queued": 12}},
    )
    qwm_alert = SimpleNamespace(
        session_name="audit-queue_without_motion-polly_remote-polly_remote",
        alert_type="audit_watchdog",
        message="Project polly_remote has 12 queued task(s) but no motion.",
    )

    assert not is_user_actionable_alert(qwm_alert, context=context)


def test_worktree_state_demotes_tracked_project_without_recent_real_work() -> None:
    from pollypm.alert_actionability import (
        AlertActionabilityContext,
        is_user_actionable_alert,
    )

    context = AlertActionabilityContext(
        known_projects=frozenset({"polly_remote"}),
        tracked_projects=frozenset({"polly_remote"}),
        recent_real_work_projects=frozenset(),
    )
    warn = SimpleNamespace(
        session_name="worker-polly_remote-9",
        alert_type="worktree_state:polly_remote/9:dirty_stale",
        message="Worker worktree has stale dirty changes.",
    )

    assert not is_user_actionable_alert(warn, context=context)


def test_orphan_worktree_alert_is_not_user_actionable() -> None:
    from pollypm.alert_actionability import is_user_actionable_alert

    alert = SimpleNamespace(
        session_name="worker-russell-58",
        alert_type="worktree_state:russell/58:orphan_branch",
        message="Worker worktree orphan branch cleanup.",
    )

    assert not is_user_actionable_alert(alert)


def test_alert_filter_task_facts_use_recent_done_for_liveness(monkeypatch) -> None:
    from pollypm import dashboard_data

    now = datetime.now(UTC)
    grouped = {
        "active": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=1),
            )
        ],
        "dormant": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=14),
            ),
            SimpleNamespace(
                work_status="queued",
                updated_at=now,
            ),
        ],
    }
    config = SimpleNamespace(projects={"active": object(), "dormant": object()})

    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_grouped",
        lambda _config: grouped,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_for_project",
        lambda data, _config, key: data[key],
    )

    facts = dashboard_data._project_task_facts_for_alert_filter(
        config,
        [
            SimpleNamespace(
                session_name="worker_session_gap-active",
                alert_type="worker_session_gap",
            )
        ],
    )

    assert facts.recent_real_work_projects == frozenset({"active"})
    assert facts.project_task_counts["dormant"]["queued"] == 1


def test_alert_filter_task_facts_excludes_synthetic_done_projects(monkeypatch) -> None:
    """#2491 — recent done rows from test debris are not brief liveness."""
    from pollypm import dashboard_data

    now = datetime.now(UTC)
    grouped = {
        "pm_test_01wave_1779715196": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=1),
            ),
            SimpleNamespace(
                work_status="queued",
                updated_at=now,
            ),
        ],
        "savethenovel": [
            SimpleNamespace(
                work_status="done",
                updated_at=now - timedelta(days=1),
            )
        ],
    }
    config = SimpleNamespace(
        projects={
            "pm_test_01wave_1779715196": SimpleNamespace(
                tracked=True,
                path="/private/tmp/pm_test_01wave_1779715196",
            ),
            "savethenovel": SimpleNamespace(
                tracked=True,
                path="/Users/sam/dev/savethenovel",
            ),
        }
    )

    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_grouped",
        lambda _config: grouped,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_for_project",
        lambda data, _config, key: data[key],
    )

    facts = dashboard_data._project_task_facts_for_alert_filter(
        config,
        [
            SimpleNamespace(
                session_name="worker_session_gap-pm_test_01wave_1779715196",
                alert_type="worker_session_gap",
            )
        ],
    )
    out = dashboard_data._build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=2,
        recent_messages=[
            dashboard_data.InboxPreview(
                sender="watchdog",
                title=(
                    "Project pm_test_01wave_1779715196 has 1 queued "
                    "task(s) but no claim / execution."
                ),
                project="pm test 01wave 1779715196",
                task_id="pm_test_01wave_1779715196/92",
                age_seconds=0.0,
                project_key="pm_test_01wave_1779715196",
            ),
            dashboard_data.InboxPreview(
                sender="watchdog",
                title=(
                    "Project savethenovel has 1 queued task(s) but no "
                    "claim / execution."
                ),
                project="Save the Novel",
                task_id="savethenovel/91",
                age_seconds=60.0,
                project_key="savethenovel",
            ),
        ],
        recovery_count_24h=0,
        recent_real_work_projects=facts.recent_real_work_projects,
    )

    assert facts.recent_real_work_projects == frozenset({"savethenovel"})
    assert facts.project_task_counts["pm_test_01wave_1779715196"]["queued"] == 1
    assert "pm_test" not in out
    assert "pm test" not in out
    assert "First up: Save the Novel has queued work without an active claim." in out


def test_dashboard_alert_count_surfaces_real_plan_stall_not_fixture(
    monkeypatch,
) -> None:
    from pollypm import dashboard_data

    grouped = {
        "media": [
            SimpleNamespace(
                work_status="queued",
                updated_at=datetime.now(UTC),
            ),
            SimpleNamespace(
                work_status="queued",
                updated_at=datetime.now(UTC),
            ),
        ],
        "pm_test_01wave_1779715196": [
            SimpleNamespace(
                work_status="queued",
                updated_at=datetime.now(UTC),
            )
        ],
    }
    config = SimpleNamespace(
        projects={
            "media": SimpleNamespace(
                tracked=True,
                path="/Users/sam/dev/media",
            ),
            "pm_test_01wave_1779715196": SimpleNamespace(
                tracked=True,
                path="/private/tmp/pm_test_01wave_1779715196",
            ),
        }
    )
    alerts = [
        SimpleNamespace(
            session_name="plan_gate-media",
            alert_type="plan_missing",
            message="Project media has 2 queued tasks but no plan.",
        ),
        SimpleNamespace(
            session_name="plan_gate-pm_test_01wave_1779715196",
            alert_type="plan_missing",
            message="Project pm_test_01wave_1779715196 has queued tasks but no plan.",
        ),
    ]

    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_grouped",
        lambda _config: grouped,
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.all_tasks_for_project",
        lambda data, _config, key: data[key],
    )

    facts = dashboard_data._project_task_facts_for_alert_filter(config, alerts)
    actionable = dashboard_data.dashboard_actionable_alerts(
        config,
        alerts,
        user_waiting_task_ids=frozenset(),
        project_task_facts=facts,
    )

    assert facts.recent_real_work_projects == frozenset()
    assert dashboard_data.count_dashboard_alerts(
        config,
        alerts,
        user_waiting_task_ids=frozenset(),
        project_task_facts=facts,
    ) == 1
    assert actionable == [alerts[0]]


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
        generated_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
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


def test_briefing_greeting_tracks_generated_time() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing

    base = {
        "commits": [],
        "completed": [],
        "inbox_count": 0,
        "recent_messages": [],
        "recovery_count_24h": 0,
    }

    morning = _build_dashboard_briefing(
        **base,
        generated_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
    )
    afternoon = _build_dashboard_briefing(
        **base,
        generated_at=datetime(2026, 6, 2, 14, 0, tzinfo=UTC),
    )
    evening = _build_dashboard_briefing(
        **base,
        generated_at=datetime(2026, 6, 2, 17, 36, tzinfo=UTC),
    )

    assert morning.startswith("Morning. Here's the overnight read.")
    assert afternoon.startswith("Afternoon. Here's where things stand.")
    assert evening.startswith("Evening. Here's where things stand.")
    assert "overnight read" not in afternoon
    assert "overnight read" not in evening


def test_briefing_splits_bookkeeping_commits_from_product_progress() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, CommitInfo

    out = _build_dashboard_briefing(
        commits=[
            CommitInfo("h1", "journal(48h): cycle 42", "a", 0.0, "pollypm"),
            CommitInfo("h2", "ledger: sync loop", "a", 0.0, "pollypm"),
            CommitInfo("h3", "fix(chat): clean PM transcript", "a", 0.0, "savethenovel"),
        ],
        completed=[],
        inbox_count=0,
        recent_messages=[],
        recovery_count_24h=0,
    )

    assert "Progress: 1 product commit (+2 bookkeeping commits) across 1 project." in out
    assert "3 commits across" not in out


def test_briefing_decomposes_remaining_inbox_projects() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, InboxPreview

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=5,
        recent_messages=[
            InboxPreview(
                sender="polly",
                title="Project savethenovel has 1 queued task but no claim / execution.",
                project="Save the Novel",
                task_id="savethenovel/1",
                age_seconds=3600,
                project_key="savethenovel",
            ),
            InboxPreview(
                sender="polly",
                title="Russell needs a decision",
                project="russell",
                task_id="russell/2",
                age_seconds=22 * 3600,
                project_key="russell",
            ),
            InboxPreview(
                sender="polly",
                title="Russell still needs a decision",
                project="russell",
                task_id="russell/3",
                age_seconds=20 * 3600,
                project_key="russell",
            ),
            InboxPreview(
                sender="polly",
                title="Russell follow-up",
                project="russell",
                task_id="russell/4",
                age_seconds=18 * 3600,
                project_key="russell",
            ),
            InboxPreview(
                sender="polly",
                title="Itsalive deploy check",
                project="itsalive",
                task_id="itsalive/5",
                age_seconds=3600,
                project_key="itsalive",
            ),
        ],
        recovery_count_24h=0,
    )

    assert "First up: Save the Novel has queued work without an active claim." in out
    assert "Remaining: russell (3, idle 22h), itsalive (1, idle 1h)." in out


def test_briefing_flags_capacity_and_recovery_strain() -> None:
    from pollypm.dashboard_data import (
        _build_dashboard_briefing,
        AccountQuotaUsage,
    )

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=0,
        recent_messages=[],
        recovery_count_24h=76,
        account_usages=[
            AccountQuotaUsage(
                account_name="claude_main",
                provider="Anthropic",
                email="sam@example.com",
                used_pct=96,
                summary="4% left",
                severity="critical",
                limit_label="weekly limit",
            )
        ],
    )

    assert "Health note: Anthropic is near its weekly limit; 76 recoveries in 24h." in out


def test_recent_pm_chat_messages_surface_persona_jsonl_line(
    monkeypatch, tmp_path,
) -> None:
    from pollypm.dashboard_data import _recent_pm_chat_messages
    from pollypm.web_api.chat import MessageEnvelope, MessageRole, MessageType, SurfaceType

    archive = tmp_path / "events.jsonl"
    archive.write_text("placeholder")
    project = SimpleNamespace(
        tracked=True,
        path="/Users/sam/dev/savethenovel",
        display_label=lambda: "Save the Novel",
    )
    config = SimpleNamespace(projects={"savethenovel": project})
    surface = SimpleNamespace(
        surface_type=SurfaceType.ARCHITECT,
        project="savethenovel",
        transcript_path=archive,
        persona="Sage",
        session_name="architect_savethenovel",
    )
    monkeypatch.setattr(
        "pollypm.web_api.chat.enumerate_chat_surfaces",
        lambda *_args, **_kwargs: [surface],
    )
    monkeypatch.setattr(
        "pollypm.web_api.chat.parse_events_jsonl_tail",
        lambda *_args, **_kwargs: [
            MessageEnvelope(
                id="tool",
                ts="2026-05-30T10:00:00Z",
                role=MessageRole.TOOL,
                actor="tool",
                type=MessageType.TOOL_RESULT,
                text="Bash(cd /tmp && cat x)",
            ),
            MessageEnvelope(
                id="sage",
                ts="2026-05-30T10:01:00Z",
                role=MessageRole.ASSISTANT,
                actor="Sage",
                type=MessageType.TEXT,
                text="I have the next outline ready for your review.",
            ),
        ],
    )

    rows = _recent_pm_chat_messages(config)

    assert len(rows) == 1
    row = rows[0]
    assert row.sender == "Sage"
    assert row.project == "Save the Novel"
    assert row.task_id == "savethenovel:pm-chat"
    assert row.title == "I have the next outline ready for your review."
    assert "Bash(" not in row.title


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


def test_briefing_surfaces_recovery_narration() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=0,
        recent_messages=[],
        recovery_count_24h=1,
        recovery_summaries=[
            "I restarted the architect for polly remote after capacity was exhausted.",
        ],
    )

    assert "While you were away, I handled this:" in out
    assert "I restarted the architect for polly remote" in out
    assert "Saved:" not in out


def test_briefing_merges_same_task_unstick_recovery_narrations() -> None:
    from pollypm.dashboard_data import (
        _build_dashboard_briefing,
        _recovery_audit_narrations_from_events,
    )

    events = [
        SimpleNamespace(
            event="watchdog.escalation_dispatched",
            metadata={
                "finding_type": "task_review_stale",
                "subject": "itsalive/55",
            },
            subject="itsalive/55",
            project="itsalive",
            status="warn",
        ),
        SimpleNamespace(
            event="watchdog.escalation_dispatched",
            metadata={
                "finding_type": "task_rework_stale",
                "subject": "itsalive/55",
            },
            subject="itsalive/55",
            project="itsalive",
            status="warn",
        ),
    ]

    summaries = _recovery_audit_narrations_from_events(
        events,
        limit=3,
        recent_real_work_projects=frozenset({"itsalive"}),
    )
    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=0,
        recent_messages=[],
        recovery_count_24h=2,
        recovery_summaries=summaries,
    )

    assert summaries == [
        "I sent unstick briefs for stale review and rework on task 55 in itsalive "
        "so the project could keep moving.",
    ]
    assert out.count("task 55") == 1
    assert "stale review and rework" in out
    assert "task rework stale" not in out


def test_briefing_first_up_skips_dormant_projects() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, InboxPreview

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=2,
        recent_messages=[
            InboxPreview(
                sender="watchdog",
                title=(
                    "Project pm_test_05wave4_1779714744 has 1 queued "
                    "task(s) but no claim / execution."
                ),
                project="pm test 05wave4 1779714744",
                task_id="pm_test_05wave4_1779714744/64",
                age_seconds=0.0,
            ),
            InboxPreview(
                sender="watchdog",
                title=(
                    "Project savethenovel has 2 queued task(s) but no "
                    "claim / execution."
                ),
                project="Save the Novel",
                task_id="savethenovel/12",
                age_seconds=60.0,
                project_key="savethenovel",
            ),
        ],
        recovery_count_24h=0,
        recent_real_work_projects=frozenset({"savethenovel"}),
    )

    assert "pm_test" not in out
    assert "pm test" not in out
    assert "First up: Save the Novel has queued work without an active claim." in out
    assert "no claim / execution" not in out
    assert "open Inbox and clear that first" in out


def test_briefing_names_dormant_operator_blocked_handoff() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, InboxPreview

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=1,
        recent_messages=[
            InboxPreview(
                sender="polly",
                title="Provide inputs to unblock Samblog Phase 0",
                project="Samblog",
                task_id="samblog/30",
                age_seconds=3600.0,
                project_key="samblog",
                allow_dormant_briefing=True,
            )
        ],
        recovery_count_24h=0,
        recent_real_work_projects=frozenset({"savethenovel"}),
        generated_at=datetime(2026, 6, 2, 17, 36, tzinfo=UTC),
    )

    assert out.startswith("Evening. Here's where things stand.")
    assert (
        "One thing needs you: Provide inputs to unblock Samblog Phase 0. "
        "Open Inbox and clear that first."
    ) in out
    assert "1 inbox item waiting" not in out


def test_briefing_first_up_skips_orphan_worktree_maintenance() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, InboxPreview

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=2,
        recent_messages=[
            InboxPreview(
                sender="worker-russell-58",
                title="Orphan worktree branch: russell/58",
                project="Russell",
                task_id="russell/58",
                age_seconds=0.0,
                project_key="russell",
            ),
            InboxPreview(
                sender="polly",
                title="Decision needed on launch copy",
                project="Save the Novel",
                task_id="savethenovel/91",
                age_seconds=60.0,
                project_key="savethenovel",
            ),
        ],
        recovery_count_24h=0,
    )

    assert "Orphan worktree branch" not in out
    assert "a task" not in out
    assert "First up: Decision needed on launch copy." in out


def test_briefing_uses_generic_inbox_line_when_only_dormant_items_exist() -> None:
    from pollypm.dashboard_data import _build_dashboard_briefing, InboxPreview

    out = _build_dashboard_briefing(
        commits=[],
        completed=[],
        inbox_count=1,
        recent_messages=[
            InboxPreview(
                sender="watchdog",
                title=(
                    "Task pm_test_05wave4_1779714744/64 has been at "
                    "status=in_progress for ~25 min with no worker heartbeat."
                ),
                project="pm test 05wave4 1779714744",
                task_id="pm_test_05wave4_1779714744/64",
                age_seconds=0.0,
            ),
        ],
        recovery_count_24h=0,
        recent_real_work_projects=frozenset({"savethenovel"}),
    )

    assert "pm_test" not in out
    assert "pm test" not in out
    assert "status=in_progress" not in out
    assert "One thing needs you: 1 inbox item waiting." in out


def test_recent_inbox_messages_reframes_cancelled_handoff_preview(
    monkeypatch, tmp_path: Path,
) -> None:
    from pollypm.dashboard_data import _recent_inbox_messages

    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    target = SimpleNamespace(
        project="samblog",
        task_number=30,
        task_id="samblog/30",
        title="Execute SamBlog Phase 0 live MCP smoke test",
        work_status=SimpleNamespace(value="blocked"),
        blocked_by=[("samblog", 26)],
        labels=[],
        roles={},
        priority=SimpleNamespace(value="high"),
        flow_template_id="chat",
        flow_template_version=1,
        current_node_id=None,
        updated_at=now,
        created_at=now,
        created_by="pm",
    )
    blocker = SimpleNamespace(
        project="samblog",
        task_number=26,
        task_id="samblog/26",
        title="Samblog Phase 0 needs your inputs",
        work_status=SimpleNamespace(value="cancelled"),
        labels=[],
        roles={"requester": "user", "operator": "user"},
        flow_template_id="chat",
        flow_template_version=1,
        current_node_id=None,
    )

    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.inbox_tasks_grouped",
        lambda _config: {"samblog": [target]},
    )
    monkeypatch.setattr(
        "pollypm.cockpit_pg_aggregates.inbox_tasks_for_project",
        lambda grouped, _config, project_key: grouped.get(project_key, []),
    )

    class _FakePgWorkService:
        closed = False

        def __init__(self, *, config) -> None:  # noqa: ARG002
            pass

        def get(self, task_id: str):
            if task_id == "samblog/26":
                return blocker
            raise KeyError(task_id)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "pollypm.work.pg_service.PgWorkService",
        _FakePgWorkService,
    )
    project = SimpleNamespace(
        tracked=True,
        path=tmp_path / "samblog",
        display_label=lambda: "Samblog",
    )
    config = SimpleNamespace(projects={"samblog": project})

    previews = _recent_inbox_messages(config)

    assert len(previews) == 1
    assert previews[0].title == "Provide inputs to unblock Samblog Phase 0"
    assert previews[0].allow_dormant_briefing is True


def test_recovery_narration_filters_dormant_projects(monkeypatch) -> None:
    from pollypm.dashboard_data import _recent_recovery_audit_narrations

    rows = [
        SimpleNamespace(
            event="watchdog.escalation_dispatched",
            ts="2026-05-30T10:00:00+00:00",
            project="pm_test_05wave4_1779714744",
            subject="pm_test_05wave4_1779714744/64",
            actor="audit_watchdog",
            status="ok",
            metadata={
                "finding_type": "stuck_draft",
                "project": "pm_test_05wave4_1779714744",
            },
        ),
        SimpleNamespace(
            event="recovery.spawn",
            ts="2026-05-30T10:05:00+00:00",
            project="savethenovel",
            subject="architect_savethenovel",
            actor="supervisor",
            status="ok",
            metadata={
                "failure_type": "capacity_exhausted",
                "target_session": "architect_savethenovel",
                "project": "savethenovel",
            },
        ),
    ]

    def fake_read_events(project: str, **_kwargs: object) -> list[object]:
        return [row for row in rows if row.project == project]

    monkeypatch.setattr("pollypm.audit.log.read_events", fake_read_events)
    config = SimpleNamespace(
        projects={
            "pm_test_05wave4_1779714744": SimpleNamespace(
                tracked=True,
                path="/tmp/pm_test",
            ),
            "savethenovel": SimpleNamespace(
                tracked=True,
                path="/tmp/savethenovel",
            ),
        }
    )

    narrations, count = _recent_recovery_audit_narrations(
        config,
        since="2026-05-30T00:00:00+00:00",
        recent_real_work_projects=frozenset({"savethenovel"}),
    )

    assert count == 2
    assert len(narrations) == 1
    assert "capacity was exhausted" in narrations[0]
    assert "pm_test" not in narrations[0]


def test_recent_recovery_audit_narrations_reads_audit_events(monkeypatch) -> None:
    from pollypm.dashboard_data import _recent_recovery_audit_narrations

    rows = [
        SimpleNamespace(
            event="recovery.spawn",
            ts="2026-05-30T10:00:00+00:00",
            project="polly_remote",
            subject="architect_polly_remote",
            actor="supervisor",
            status="ok",
            metadata={
                "failure_type": "capacity_exhausted",
                "target_session": "architect_polly_remote",
                "project": "polly_remote",
            },
        )
    ]

    monkeypatch.setattr("pollypm.audit.log.read_events", lambda *a, **k: rows)
    config = SimpleNamespace(
        projects={
            "polly_remote": SimpleNamespace(
                tracked=True,
                path="/tmp/polly_remote",
            )
        }
    )

    narrations, count = _recent_recovery_audit_narrations(
        config,
        since="2026-05-30T00:00:00+00:00",
    )

    assert count == 1
    assert narrations == [
        "I restarted the architect for polly remote after capacity was exhausted.",
    ]


def test_recent_recovery_audit_narrations_uses_bounded_tail(
    monkeypatch,
) -> None:
    from pollypm.dashboard_data import (
        _RECOVERY_AUDIT_TAIL_LIMIT_PER_PROJECT,
        _recent_recovery_audit_narrations,
    )

    calls: list[dict[str, object]] = []
    rows = [
        SimpleNamespace(
            event="recovery.spawn",
            ts="2026-05-29T23:00:00+00:00",
            project="polly_remote",
            subject="architect_polly_remote",
            actor="supervisor",
            status="ok",
            metadata={"target_session": "architect_polly_remote"},
        ),
        SimpleNamespace(
            event="recovery.spawn",
            ts="2026-05-30T10:00:00+00:00",
            project="polly_remote",
            subject="architect_polly_remote",
            actor="supervisor",
            status="ok",
            metadata={"target_session": "architect_polly_remote"},
        ),
    ]

    def fake_read_events(*_args: object, **kwargs: object) -> list[object]:
        calls.append(kwargs)
        return rows

    monkeypatch.setattr("pollypm.audit.log.read_events", fake_read_events)
    config = SimpleNamespace(
        projects={
            "polly_remote": SimpleNamespace(
                tracked=True,
                path="/tmp/polly_remote",
            )
        }
    )

    narrations, count = _recent_recovery_audit_narrations(
        config,
        since="2026-05-30T00:00:00+00:00",
    )

    assert calls == [
        {
            "limit": _RECOVERY_AUDIT_TAIL_LIMIT_PER_PROJECT,
            "project_path": "/tmp/polly_remote",
        }
    ]
    assert count == 1
    assert narrations == [
        "I restarted the architect for polly remote as part of recovery.",
    ]


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
