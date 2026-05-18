"""#1631 — duplicate-window mount picks the live-provider pane.

When park-collisions leave two ``pm-operator`` windows in the storage
closet (one stale empty, one with the user's live Polly conversation),
the rail's mount code must pick the live one. The pre-#1631 mount used
``next(w for w in windows if w.name == window_name)`` which always
picked the lowest-index window — and in Sam's repro that was the empty
placeholder, so clicking back to Polly silently mounted onto a fresh
``claude`` session and the in-progress conversation was orphaned in the
other (higher-index) window. #1563's conservative cleanup correctly
refused to kill either, so the loss became persistent.

These tests pin the new contract for ``_select_storage_window_for_mount``:
prefer ``claude`` / ``codex`` / ``node`` panes; do not kill the loser;
emit a ``cockpit.duplicate_resolution`` audit event so forensics can
trace which window won and why.
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


def test_no_match_returns_none() -> None:
    router = _bare_router()
    emitted = _capture_audit(router)
    result = router._select_storage_window_for_mount(
        [_window(index=0, name="pm-russell")],
        "pm-operator",
        "operator",
    )
    assert result is None
    assert emitted == []


def test_single_match_returns_it_without_audit() -> None:
    router = _bare_router()
    emitted = _capture_audit(router)
    only = _window(index=1, name="pm-operator", pane_current_command="claude")
    result = router._select_storage_window_for_mount(
        [only], "pm-operator", "operator",
    )
    assert result is only
    # No duplicate → no audit noise. Common path.
    assert emitted == []


def test_duplicate_prefers_live_claude_over_empty_placeholder() -> None:
    """The smoking-gun scenario from #1631.

    Index 1 is an empty placeholder shell. Index 42 is the user's
    in-progress Polly conversation running ``claude``. The pre-fix code
    picked index 1 and wiped the conversation. The fix picks index 42.
    """
    router = _bare_router()
    emitted = _capture_audit(router)
    placeholder = _window(index=1, name="pm-operator", pane_current_command="zsh")
    live = _window(index=42, name="pm-operator", pane_current_command="claude")
    result = router._select_storage_window_for_mount(
        [placeholder, live, _window(index=2, name="pm-russell")],
        "pm-operator",
        "operator",
    )
    assert result is live
    assert len(emitted) == 1
    event = emitted[0]
    assert event["event"] == "cockpit.duplicate_resolution"
    assert event["subject"] == "pm-operator"
    assert event["status"] == "warn"
    assert event["metadata"]["chosen_index"] == 42
    assert event["metadata"]["chosen_command"] == "claude"
    assert event["metadata"]["reason"] == "live_provider_pane"
    assert sorted(event["metadata"]["all_indices"]) == [1, 42]
    assert event["metadata"]["live_provider_indices"] == [42]


def test_duplicate_prefers_codex_node_pane() -> None:
    """Codex panes show up as ``node`` in tmux."""
    router = _bare_router()
    _capture_audit(router)
    placeholder = _window(index=0, name="pm-operator", pane_current_command="bash")
    live = _window(index=5, name="pm-operator", pane_current_command="node")
    result = router._select_storage_window_for_mount(
        [placeholder, live], "pm-operator", "operator",
    )
    assert result is live


def test_duplicate_with_two_live_providers_picks_lowest_index() -> None:
    """Tie-break: when both windows have a live provider, pick lowest index.

    This is the truly-ambiguous case. Deterministic tie-break keeps
    behavior reproducible; the operator can investigate via the audit
    event metadata.
    """
    router = _bare_router()
    emitted = _capture_audit(router)
    a = _window(index=3, name="pm-operator", pane_current_command="claude")
    b = _window(index=7, name="pm-operator", pane_current_command="claude")
    result = router._select_storage_window_for_mount(
        [b, a], "pm-operator", "operator",
    )
    assert result is a  # lower index wins
    assert emitted[0]["metadata"]["reason"] == "live_provider_pane"
    assert emitted[0]["metadata"]["chosen_index"] == 3


def test_duplicate_with_no_live_provider_falls_back_to_non_dead() -> None:
    """When neither pane runs claude/codex, prefer the non-dead one.

    This is a degraded-path safety net — the user still gets a usable
    pane rather than a hard refusal to mount.
    """
    router = _bare_router()
    emitted = _capture_audit(router)
    dead = _window(index=0, name="pm-operator", pane_current_command="zsh", pane_dead=True)
    alive_shell = _window(index=8, name="pm-operator", pane_current_command="zsh")
    result = router._select_storage_window_for_mount(
        [dead, alive_shell], "pm-operator", "operator",
    )
    assert result is alive_shell
    assert emitted[0]["metadata"]["reason"] == "non_dead_fallback"


def test_select_never_kills_loser() -> None:
    """The selector picks a winner but must NOT kill the loser.

    #1563's conservative cleanup is responsible for reaping the
    duplicate after its pane dies. The selector only resolves which
    window to attach to. Killing here re-introduces the #1562 wipe by a
    different code path.
    """

    class _TrackingTmux:
        def __init__(self) -> None:
            self.killed: list[str] = []

        def kill_window(self, target: str) -> None:
            self.killed.append(target)

    router = _bare_router()
    router.tmux = _TrackingTmux()  # type: ignore[attr-defined]
    _capture_audit(router)
    placeholder = _window(index=1, name="pm-operator", pane_current_command="zsh")
    live = _window(index=42, name="pm-operator", pane_current_command="claude")
    router._select_storage_window_for_mount(
        [placeholder, live], "pm-operator", "operator",
    )
    assert router.tmux.killed == []
