"""#1636 — project PM-persona sibling-pattern fallback for the mount selector.

#1632 / #1631 fixed the cockpit-rail mount path so duplicate ``pm-operator``
windows in the storage closet resolve to the live one. The fix routes a
literal ``window_name`` lookup through ``_select_storage_window_for_mount``
and picks the pane that's actually running ``claude`` / ``codex`` / ``node``.

That left Sam's bikepath repro unfixed: the rail mount target was the
project's PM chat, but ``launch.window_name`` pointed at ``worker-bikepath``
(or wasn't present in the planner at all) while the live PM conversation
lived in ``architect-bikepath``. The literal-only selector returned None,
the caller respawned a fresh codex, and Sam's 44-minute conversation got
orphaned in a storage window he could no longer see.

These tests pin the #1636 sibling-pattern fallback: when ``project_key`` is
supplied and the literal ``window_name`` produces no match, the selector
tries the project's canonical PM-persona window patterns in priority order
(``architect-<project>`` first, then ``pm-<project>``, then
``worker-<project>``) and returns the first one that's running a live
provider. The selector also exposes a ``_project_pm_sibling_window_live``
probe so the routing layer can skip ``supervisor.launch_session`` when a
sibling is already serving the user's conversation.
"""

from __future__ import annotations

from pollypm.cockpit_rail import CockpitRouter
from pollypm.tmux.client import TmuxWindow


def _window(
    *,
    index: int,
    name: str,
    pane_current_command: str = "zsh",
    pane_dead: bool = False,
    session: str = "pollypm-storage-closet",
) -> TmuxWindow:
    return TmuxWindow(
        session=session,
        index=index,
        name=name,
        active=False,
        pane_id=f"%{index}",
        pane_current_command=pane_current_command,
        pane_current_path="/tmp",
        pane_dead=pane_dead,
        pane_pid=10_000 + index,
    )


def _bare_router() -> CockpitRouter:
    router = CockpitRouter.__new__(CockpitRouter)
    return router


def _capture_audit(router: CockpitRouter) -> list[dict]:
    emitted: list[dict] = []

    def _record(*, event_name, subject, status, metadata):
        emitted.append(
            {
                "event": event_name,
                "subject": subject,
                "status": status,
                "metadata": metadata,
            }
        )

    router._emit_cockpit_audit = _record  # type: ignore[assignment]
    return emitted


def test_no_project_key_keeps_literal_only_behavior() -> None:
    """Without ``project_key``, the selector behaves exactly as #1631 did.

    Regression guard: callers that don't know the project (Polly,
    Russell, task windows) must not accidentally pick up an unrelated
    project's PM window.
    """
    router = _bare_router()
    _capture_audit(router)
    architect = _window(index=5, name="architect-bikepath", pane_current_command="codex")
    result = router._select_storage_window_for_mount(
        [architect], "worker-bikepath", "worker_bikepath",
    )
    assert result is None  # no project_key → no fallback


def test_project_pm_fallback_prefers_architect_over_worker() -> None:
    """Sam's bikepath repro.

    ``launch.window_name`` is ``worker-bikepath`` (the planner picked
    the worker entry) but the live PM conversation is running codex in
    ``architect-bikepath``. The selector must attach to the architect,
    not return None and let the caller spawn a fresh codex.
    """
    router = _bare_router()
    emitted = _capture_audit(router)
    architect = _window(
        index=38, name="architect-bikepath", pane_current_command="codex",
    )
    # An unrelated project's window must not be picked up.
    unrelated = _window(index=10, name="architect-other", pane_current_command="codex")
    result = router._select_storage_window_for_mount(
        [architect, unrelated],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result is architect
    assert len(emitted) == 1
    event = emitted[0]
    assert event["event"] == "cockpit.project_pm_fallback_match"
    assert event["subject"] == "worker-bikepath"
    assert event["metadata"]["matched_window_name"] == "architect-bikepath"
    assert event["metadata"]["matched_index"] == 38
    assert event["metadata"]["matched_command"] == "codex"
    assert event["metadata"]["project_key"] == "bikepath"
    assert event["metadata"]["reason"] == "project_pm_sibling_pattern"


def test_project_pm_fallback_skips_idle_sibling_shells() -> None:
    """A stale ``architect-<project>`` shell (zsh, no codex) is worse than None.

    Joining an idle shell would tear down the user's cockpit pane to
    mount a dead-pane placeholder. The caller's fallback (static project
    view) is the safer surface.
    """
    router = _bare_router()
    emitted = _capture_audit(router)
    idle_shell = _window(
        index=12, name="architect-bikepath", pane_current_command="zsh",
    )
    result = router._select_storage_window_for_mount(
        [idle_shell],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result is None
    # No fallback-match audit because we refused the sibling.
    fallback_events = [
        e for e in emitted if e["event"] == "cockpit.project_pm_fallback_match"
    ]
    assert fallback_events == []


def test_project_pm_fallback_skips_dead_sibling() -> None:
    """Dead panes are never preferred over a clean None."""
    router = _bare_router()
    _capture_audit(router)
    dead_architect = _window(
        index=4,
        name="architect-bikepath",
        pane_current_command="codex",
        pane_dead=True,
    )
    result = router._select_storage_window_for_mount(
        [dead_architect],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result is None


def test_project_pm_fallback_walks_priority_order() -> None:
    """PM > architect > worker.

    Per-project-pm rollout: ``pm-<project>`` is now the canonical
    per-project PM persona window (auto-injected by the launch
    planner), so it wins over the historical ``architect-<project>``
    fallback. The architect/worker fallbacks remain for installs that
    haven't picked up the per-project PM yet (no compatible account
    routing, etc.) and for legacy non-Polly projects where the
    architect window held the ongoing PM conversation pre-#1636.
    """
    router = _bare_router()
    _capture_audit(router)
    worker = _window(
        index=2, name="worker-bikepath", pane_current_command="codex",
    )
    pm = _window(
        index=3, name="pm-bikepath", pane_current_command="codex",
    )
    architect = _window(
        index=4, name="architect-bikepath", pane_current_command="codex",
    )
    result = router._select_storage_window_for_mount(
        [worker, pm, architect],
        "worker-bikepath",  # literal would match worker, but we want pm
        "worker_bikepath",
        project_key="bikepath",
    )
    # The literal lookup matched ``worker-bikepath`` directly (single
    # match), so the sibling fallback never fired — this asserts the
    # fallback only triggers when the literal lookup fails.
    assert result is worker

    # Now remove the worker window entirely — the literal lookup fails
    # and the fallback walks the priority order, picking ``pm-bikepath``
    # (the canonical per-project PM persona).
    result_no_worker = router._select_storage_window_for_mount(
        [pm, architect],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result_no_worker is pm

    # Drop the pm window too — fall through to architect-<project>.
    result_no_pm = router._select_storage_window_for_mount(
        [architect],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result_no_pm is architect


def test_project_pm_fallback_falls_through_to_pm_when_no_architect() -> None:
    """When no architect window exists, fall back to ``pm-<project>``."""
    router = _bare_router()
    emitted = _capture_audit(router)
    pm = _window(
        index=6, name="pm-bikepath", pane_current_command="codex",
    )
    result = router._select_storage_window_for_mount(
        [pm],
        "worker-bikepath",
        "worker_bikepath",
        project_key="bikepath",
    )
    assert result is pm
    fallback_events = [
        e for e in emitted if e["event"] == "cockpit.project_pm_fallback_match"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0]["metadata"]["matched_window_name"] == "pm-bikepath"


def test_project_pm_sibling_window_live_finds_architect() -> None:
    """The pre-mount probe must report True when a live PM sibling exists.

    This is the gate that prevents ``_route_project_selection`` from
    calling ``supervisor.launch_session`` (which would respawn the
    planner-named window, killing the live one in the process).
    """
    router = _bare_router()
    architect = _window(
        index=38, name="architect-bikepath", pane_current_command="codex",
    )

    class _FakeSupervisor:
        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class _FakeTmux:
        def list_windows(self, _session: str) -> list[TmuxWindow]:
            return [architect]

    router.tmux = _FakeTmux()  # type: ignore[attr-defined]
    assert router._project_pm_sibling_window_live(_FakeSupervisor(), "bikepath") is True


def test_project_pm_sibling_window_live_ignores_idle_shells() -> None:
    """An idle ``architect-<project>`` shell does NOT count as a live PM.

    If we treated it as live we'd skip ``launch_session`` and then
    fail to mount anything useful — the user would see no PM at all,
    a worse failure than the original respawn.
    """
    router = _bare_router()
    idle_shell = _window(
        index=12, name="architect-bikepath", pane_current_command="zsh",
    )

    class _FakeSupervisor:
        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class _FakeTmux:
        def list_windows(self, _session: str) -> list[TmuxWindow]:
            return [idle_shell]

    router.tmux = _FakeTmux()  # type: ignore[attr-defined]
    assert router._project_pm_sibling_window_live(_FakeSupervisor(), "bikepath") is False


def test_project_pm_sibling_window_live_handles_supervisor_failure() -> None:
    """Tmux / supervisor exceptions degrade to False, not a hard crash.

    A flaky list_windows call must never block the mount path; the
    caller will see ``False`` and fall through to its existing
    ``launch_session`` path — same behavior as before #1636.
    """
    router = _bare_router()

    class _FakeSupervisor:
        def storage_closet_session_name(self) -> str:
            raise RuntimeError("tmux server gone")

    assert router._project_pm_sibling_window_live(_FakeSupervisor(), "bikepath") is False


def test_route_project_selection_skips_launch_when_architect_sibling_is_live() -> None:
    """#1636 integration regression: bikepath PM from rail → attach to
    architect-bikepath, do NOT spawn a fresh codex via launch_session.

    Setup matches the issue forensics:
      * Planner returns ``worker_bikepath`` (worker entry in config).
      * Worker window is NOT present in storage.
      * ``architect-bikepath`` IS present in storage, pane running codex
        (the user's live 44-minute conversation).

    Pre-#1636 behavior: ``_session_available_for_mount`` returns False
    because ``worker-bikepath`` isn't in storage, so the route calls
    ``supervisor.launch_session`` which spawns a fresh codex window with
    the bootstrap prompt — wiping the user's context from view.

    Post-#1636 behavior: the sibling-pattern probe sees the live
    architect window and short-circuits before ``launch_session`` is
    called. ``_show_live_session`` (also exercised via the fallback
    path) is what actually attaches to it.
    """
    from pathlib import Path

    from pollypm.cockpit_rail_routes import ProjectRoute

    launch_session_calls: list[str] = []
    show_live_session_calls: list[tuple[str, str]] = []

    architect_window = _window(
        index=38, name="architect-bikepath", pane_current_command="codex",
    )

    class _FakeTmux:
        def list_windows(self, _session: str) -> list[TmuxWindow]:
            return [architect_window]

        def send_keys(self, *_a, **_k):  # primer plumbing
            pass

    class _Sess:
        name = "worker_bikepath"
        role = "worker"
        project = "bikepath"

    class _Launch:
        session = _Sess()
        window_name = "worker-bikepath"

    class _Project:
        key = "bikepath"
        name = "bikepath"
        path = Path("/tmp/bikepath")
        persona_name = None

    class _FakeConfig:
        projects = {"bikepath": _Project()}

    class _FakeSupervisor:
        config = _FakeConfig()

        def plan_launches(self):
            return [_Launch()]

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def launch_session(self, session_name: str):
            launch_session_calls.append(session_name)

    router = _bare_router()
    router.tmux = _FakeTmux()  # type: ignore[attr-defined]
    router.config_path = Path("/tmp/pollypm.toml")
    router._right_pane_id = lambda _window_target: "%right"  # type: ignore[assignment]
    router._load_state = lambda: {}  # type: ignore[assignment]
    router._write_state = lambda _data: None  # type: ignore[assignment]
    router.set_selected_key = lambda _key: None  # type: ignore[assignment]
    router._show_static_view = lambda *_a, **_k: None  # type: ignore[assignment]
    router._maybe_prime_project_pm_session = lambda *_a, **_k: None  # type: ignore[assignment]

    def _record_show_live_session(_supervisor, session_name, window_target):
        show_live_session_calls.append((session_name, window_target))

    router._show_live_session = _record_show_live_session  # type: ignore[assignment]

    # ``_session_available_for_mount`` returns False because the
    # planner-named ``worker-bikepath`` window doesn't exist in storage
    # (only ``architect-bikepath`` does). Pre-#1636 this triggered
    # ``launch_session`` — the regression we're guarding against.
    supervisor = _FakeSupervisor()
    router._session_available_for_mount = lambda *_a, **_k: False  # type: ignore[assignment]

    router._route_project_selection(
        supervisor,
        "pollypm:PollyPM",
        ProjectRoute(project_key="bikepath", sub_view="session"),
    )

    # The critical assertion: launch_session was NOT called, so the
    # live architect-bikepath codex was not killed off.
    assert launch_session_calls == [], (
        "launch_session must be skipped when a live PM sibling exists in storage"
    )
    # ``_show_live_session`` is still invoked so the attach happens via
    # the sibling-pattern fallback inside ``_select_storage_window_for_mount``.
    assert show_live_session_calls == [("worker_bikepath", "pollypm:PollyPM")]
