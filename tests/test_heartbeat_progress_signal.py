"""Regression tests for the #1501 progress-signal accounting.

The heartbeat distinguishes "alive but silent" from "actively producing
output." Workers (and other non-event-driven roles) that go ``≥
_QUIET_TICKS_FOR_FLOW_MARKER`` ticks without fresh transcript output
get their reason prefixed with ``flow=quiet:`` so ``pm status`` shows
the marker. Event-driven roles never accumulate quiet ticks.

This generalises the per-question detection added in #1493 (heartbeat-
question-detect): #1493 catches the specific "agent asked operator a
question" stall; this fix catches every other variety where the pane
just sits silent.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from pollypm.heartbeats.api import SupervisorHeartbeatAPI
from pollypm.heartbeats.base import HeartbeatCursor, HeartbeatSessionContext
from pollypm.heartbeats.local import (
    FLOW_QUIET_REASON_PREFIX,
    LocalHeartbeatBackend,
    _PROGRESS_SIGNAL_EXEMPT_ROLES,
    _QUIET_TICKS_FOR_FLOW_MARKER,
    _NO_TRANSCRIPT_REASON,
)


# ---------------------------------------------------------------------------
# HeartbeatCursor / api.update_cursor — persistence layer
# ---------------------------------------------------------------------------


def test_cursor_default_quiet_tick_count_is_zero() -> None:
    cursor = HeartbeatCursor(
        session_name="sess",
        source_path="/tmp/sess.log",
        last_offset=0,
    )
    assert cursor.quiet_tick_count == 0


def test_cursor_round_trips_quiet_tick_count() -> None:
    """asdict / from-dict survives the new field — the cursor JSON
    file uses dict round-trip, so a missing-key payload from a
    pre-#1501 install must still parse."""
    cursor = HeartbeatCursor(
        session_name="sess",
        source_path="/tmp/sess.log",
        last_offset=0,
        quiet_tick_count=3,
    )
    payload = asdict(cursor)
    assert payload["quiet_tick_count"] == 3
    rehydrated = HeartbeatCursor(**payload)
    assert rehydrated.quiet_tick_count == 3


def test_cursor_old_payload_without_quiet_tick_count_loads() -> None:
    """A cursors.json file written by an older heartbeat MUST still
    deserialise — quiet_tick_count has a default of 0."""
    legacy_payload = {
        "session_name": "sess",
        "source_path": "/tmp/sess.log",
        "last_offset": 5,
        "last_processed_at": None,
        "last_snapshot_hash": "",
        "last_verdict": "",
        "last_reason": "",
    }
    cursor = HeartbeatCursor(**legacy_payload)
    assert cursor.quiet_tick_count == 0


def test_update_cursor_persists_quiet_tick_count(tmp_path: Path) -> None:
    """Real persistence path through SupervisorHeartbeatAPI — proves
    the new kwarg threads through ``_save_cursors`` / ``_load_cursors``
    without any field loss."""
    api = _persistence_api(tmp_path)
    api.update_cursor(
        "worker_x",
        source_path=str(tmp_path / "worker_x.log"),
        last_offset=42,
        verdict="unclear",
        reason=_NO_TRANSCRIPT_REASON,
        quiet_tick_count=2,
    )
    reloaded = api.get_cursor("worker_x")
    assert reloaded is not None
    assert reloaded.quiet_tick_count == 2
    assert reloaded.last_offset == 42


def test_update_cursor_default_quiet_tick_count_is_zero(tmp_path: Path) -> None:
    """Existing callers (e.g. the missing-window branch in
    ``_process_session``) don't pass ``quiet_tick_count`` — the
    default must be 0 so they keep compiling unchanged."""
    api = _persistence_api(tmp_path)
    api.update_cursor(
        "worker_x",
        source_path=str(tmp_path / "worker_x.log"),
        last_offset=10,
        verdict="missing_window",
        reason="Expected tmux window is missing",
    )
    reloaded = api.get_cursor("worker_x")
    assert reloaded is not None
    assert reloaded.quiet_tick_count == 0


# ---------------------------------------------------------------------------
# Module-level constants — pin the public surface that ``pm status`` and
# the cockpit pill renderers must match against.
# ---------------------------------------------------------------------------


def test_flow_quiet_reason_prefix_is_stable() -> None:
    """The cockpit + pm-status renderers use this prefix to detect the
    marker. Changing it is a UX-visible break; pin the literal."""
    assert FLOW_QUIET_REASON_PREFIX == "flow=quiet: "


def test_quiet_threshold_matches_three_ticks() -> None:
    """3 ticks ≈ 90s of silence (assuming 30s heartbeat cadence) — long
    enough to filter a long thinking pause, short enough that an actual
    stall surfaces before the operator notices."""
    assert _QUIET_TICKS_FOR_FLOW_MARKER == 3


def test_progress_exempt_roles_cover_event_driven_set() -> None:
    """The exempt-role set must include every role that
    ``stall_classifier._EVENT_DRIVEN_ROLES`` exempts, plus architects
    (which are also event-driven per stall_classifier:168). If
    stall_classifier widens its exempt set, this test surfaces the
    drift so the progress-signal accounting can mirror it."""
    from pollypm.heartbeats.stall_classifier import _EVENT_DRIVEN_ROLES

    assert _EVENT_DRIVEN_ROLES <= _PROGRESS_SIGNAL_EXEMPT_ROLES
    assert "architect" in _PROGRESS_SIGNAL_EXEMPT_ROLES


# ---------------------------------------------------------------------------
# _process_session — quiet-tick accounting integrated with the dispatch
# ---------------------------------------------------------------------------


def test_worker_with_no_new_transcript_increments_quiet_counter() -> None:
    """Three consecutive ticks of ``_NO_TRANSCRIPT_REASON`` for a
    worker promotes the reason to ``flow=quiet: …``. The first two
    ticks accumulate; the third surfaces the marker."""
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="worker_x",
        source_path="/tmp/worker_x.log",
        last_offset=64,
        quiet_tick_count=2,
    )
    context = _quiet_worker_context(cursor=cursor)
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 3
    final_status, final_reason = api.statuses["worker_x"]
    assert final_status == "healthy"
    assert final_reason.startswith(FLOW_QUIET_REASON_PREFIX)
    assert _NO_TRANSCRIPT_REASON in final_reason


def test_worker_under_threshold_does_not_get_flow_quiet_marker() -> None:
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="worker_x",
        source_path="/tmp/worker_x.log",
        last_offset=64,
        quiet_tick_count=0,
    )
    context = _quiet_worker_context(cursor=cursor)
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 1
    _, final_reason = api.statuses["worker_x"]
    assert FLOW_QUIET_REASON_PREFIX not in final_reason


def test_fresh_transcript_resets_quiet_counter() -> None:
    """Worker that produced fresh output this tick — even if it had
    been quiet for 10 ticks before — gets the counter reset to 0."""
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="worker_x",
        source_path="/tmp/worker_x.log",
        last_offset=64,
        quiet_tick_count=10,
    )
    context = _active_worker_context(cursor=cursor)
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 0
    _, final_reason = api.statuses["worker_x"]
    assert FLOW_QUIET_REASON_PREFIX not in final_reason


def test_event_driven_role_never_accumulates_quiet_ticks() -> None:
    """The heartbeat-supervisor role itself sits silent by design;
    accumulating quiet ticks would falsely flag it ``flow=quiet`` on
    every tick. Counter pinned to 0."""
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="heartbeat",
        source_path="/tmp/heartbeat.log",
        last_offset=64,
        quiet_tick_count=99,
    )
    context = _quiet_worker_context(
        cursor=cursor,
        session_name="heartbeat",
        role="heartbeat-supervisor",
    )
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 0
    _, final_reason = api.statuses["heartbeat"]
    assert FLOW_QUIET_REASON_PREFIX not in final_reason


def test_architect_never_accumulates_quiet_ticks() -> None:
    """Architects are event-driven (stall_classifier:168). They sit
    quiet between plan emits — must not flow=quiet."""
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="architect_x",
        source_path="/tmp/architect_x.log",
        last_offset=64,
        quiet_tick_count=99,
    )
    context = _quiet_worker_context(
        cursor=cursor,
        session_name="architect_x",
        role="architect",
    )
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 0


def test_classifier_non_unclear_verdict_resets_counter() -> None:
    """Worker whose transcript classifies as e.g. ``needs_followup``
    or ``blocked`` is producing meaningful output — reset counter
    even though our broader heuristics might still treat it as quiet."""
    api = _ProcessSessionFakeAPI()
    backend = LocalHeartbeatBackend()
    cursor = HeartbeatCursor(
        session_name="worker_x",
        source_path="/tmp/worker_x.log",
        last_offset=64,
        quiet_tick_count=5,
    )
    context = _classified_blocked_worker_context(cursor=cursor)
    backend._process_session(api, context)

    update = api.cursor_updates[-1]
    assert update["quiet_tick_count"] == 0


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _persistence_api(tmp_path: Path) -> SupervisorHeartbeatAPI:
    """Build a SupervisorHeartbeatAPI shell that uses a writable
    cursors.json rooted at tmp_path. We bypass ``__init__`` (which
    eagerly walks the supervisor's window/launch map) because the
    cursor-persistence tests only exercise update_cursor / get_cursor,
    not session enumeration."""
    api = SupervisorHeartbeatAPI.__new__(SupervisorHeartbeatAPI)
    api.supervisor = SimpleNamespace(
        config=SimpleNamespace(
            project=SimpleNamespace(base_dir=tmp_path),
        ),
    )
    api._contexts = []
    api._unmanaged = []
    return api


class _ProcessSessionFakeAPI:
    """Minimal API stub for exercising ``_process_session``'s quiet-
    tick accounting end-to-end. Records the cursor-update kwargs and
    every set_session_status call so tests can assert on the final
    state.

    Stub methods raise no exceptions — _process_session swallows
    intervention-engine failures internally, but the data-flow paths
    we care about (set_session_status + update_cursor) must succeed.
    """

    def __init__(self) -> None:
        self.cursor_updates: list[dict[str, object]] = []
        self.statuses: dict[str, tuple[str, str]] = {}
        self.alerts: dict[tuple[str, str], object] = {}
        self.checkpoints: list[tuple[str, list[str]]] = []
        self.supervisor = SimpleNamespace(
            store=SimpleNamespace(
                get_session_runtime=lambda _name: None,
            ),
            config=SimpleNamespace(sessions={}, projects={}),
            msg_store=None,
        )

    def list_sessions(self):
        return []

    def list_unmanaged_windows(self):
        return []

    def update_cursor(
        self,
        session_name: str,
        *,
        source_path: str,
        last_offset: int,
        snapshot_hash: str = "",
        verdict: str = "",
        reason: str = "",
        quiet_tick_count: int = 0,
    ) -> None:
        self.cursor_updates.append({
            "session_name": session_name,
            "source_path": source_path,
            "last_offset": last_offset,
            "snapshot_hash": snapshot_hash,
            "verdict": verdict,
            "reason": reason,
            "quiet_tick_count": quiet_tick_count,
        })

    def record_observation(self, context) -> None:
        pass

    def record_checkpoint(self, context, *, alerts) -> None:
        self.checkpoints.append((context.session_name, list(alerts)))

    def record_event(self, *_args, **_kwargs) -> None:
        pass

    def raise_alert(self, *_args, **_kwargs) -> None:
        pass

    def clear_alert(self, session_name, alert_type) -> None:
        self.alerts.pop((session_name, alert_type), None)

    def open_alerts(self):
        return []

    def set_session_status(self, session_name, status, *, reason="") -> None:
        self.statuses[session_name] = (status, reason)

    def mark_account_auth_broken(self, *_args, **_kwargs) -> None:
        pass

    def recent_snapshot_hashes(self, *_args, **_kwargs):
        return []

    def recover_session(self, *_args, **_kwargs) -> None:
        pass

    def send_session_message(self, *_args, **_kwargs) -> None:
        pass

    def queue_polly_followup(self, *_args, **_kwargs) -> None:
        pass


def _quiet_worker_context(
    *,
    cursor: HeartbeatCursor,
    session_name: str = "worker_x",
    role: str = "worker",
) -> HeartbeatSessionContext:
    """Worker pane that produced no fresh transcript content this
    tick — same source_bytes as the cursor's last_offset, empty
    transcript_delta. ``_classify`` will return
    ``("unclear", _NO_TRANSCRIPT_REASON)``."""
    return HeartbeatSessionContext(
        session_name=session_name,
        role=role,
        project_key="proj",
        provider="claude",
        account_name="acct",
        cwd="/workspace",
        tmux_session="ses",
        window_name=session_name,
        source_path="/tmp/worker_x.log",
        source_bytes=cursor.last_offset,
        transcript_delta="",
        pane_text="❯ ",
        snapshot_path="/tmp/snap.txt",
        snapshot_hash="hash-1",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        window_present=True,
        previous_log_bytes=cursor.last_offset,
        previous_snapshot_hash=cursor.last_snapshot_hash,
        cursor=cursor,
    )


def _active_worker_context(*, cursor: HeartbeatCursor) -> HeartbeatSessionContext:
    """Worker pane that produced fresh content. ``_classify`` returns
    a real verdict (``done`` for a complete-looking turn)."""
    return HeartbeatSessionContext(
        session_name="worker_x",
        role="worker",
        project_key="proj",
        provider="claude",
        account_name="acct",
        cwd="/workspace",
        tmux_session="ses",
        window_name="worker_x",
        source_path="/tmp/worker_x.log",
        source_bytes=cursor.last_offset + 100,
        transcript_delta="Implemented the change and shipped it.",
        pane_text="Implemented the change and shipped it.\n❯ ",
        snapshot_path="/tmp/snap.txt",
        snapshot_hash="hash-2",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        window_present=True,
        previous_log_bytes=cursor.last_offset,
        previous_snapshot_hash=cursor.last_snapshot_hash,
        cursor=cursor,
    )


def _classified_blocked_worker_context(
    *, cursor: HeartbeatCursor
) -> HeartbeatSessionContext:
    """Worker whose new transcript content classifies as ``blocked``
    (waiting on operator). Tests that the counter resets on any
    non-``unclear`` verdict, not just the success cases."""
    return HeartbeatSessionContext(
        session_name="worker_x",
        role="worker",
        project_key="proj",
        provider="claude",
        account_name="acct",
        cwd="/workspace",
        tmux_session="ses",
        window_name="worker_x",
        source_path="/tmp/worker_x.log",
        source_bytes=cursor.last_offset + 50,
        transcript_delta="Waiting on operator: should I commit?",
        pane_text="Waiting on operator: should I commit?\n❯ ",
        snapshot_path="/tmp/snap.txt",
        snapshot_hash="hash-2",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        window_present=True,
        previous_log_bytes=cursor.last_offset,
        previous_snapshot_hash=cursor.last_snapshot_hash,
        cursor=cursor,
    )
