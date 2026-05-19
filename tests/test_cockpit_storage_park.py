"""Regression tests for :mod:`pollypm.cockpit_storage_park` (#1631 follow-up).

#1631 — Sam lost an in-progress Polly conversation by clicking away and
back.  The repro generalises to any per-project PM window
(``architect-<project>`` for non-Polly projects).  #1635 fixed the
park-side bug in ``_park_mounted_session`` but three other call sites
(``cockpit_window_manager.park_live_to_storage``,
``core/console_window.py``, ``cockpit_rail.ensure_cockpit_layout``)
still called ``tmux break-pane`` without the dup-check, so the same
wipe surface was reachable through those paths.

These tests pin the contract of :func:`safe_break_pane_to_storage`:

* Live duplicate in storage → break-pane is skipped, the source pane
  is killed (no orphan pile-up), the audit fires, and the helper
  returns ``False`` so callers can branch.
* Dead duplicates → killed before break-pane so the freshly-parked
  pane re-occupies the canonical name.
* No duplicates → unconditional break-pane.

The smoking-gun scenario from Sam's 2026-05-19 wipe (architect-samblog
duplicated at indices 22 and 42 in the closet) is reproduced as
``test_safe_break_does_not_add_third_when_two_duplicates_exist``.
"""

from __future__ import annotations

from typing import Any

from pollypm.cockpit_storage_park import safe_break_pane_to_storage


class _FakeWindow:
    """Minimal tmux window shape used by the helper.

    Mirrors the ``TmuxWindow`` dataclass surface the helper inspects:
    ``index``, ``name``, ``pane_current_command``, ``pane_dead``.
    """

    def __init__(
        self,
        index: int,
        name: str,
        command: str = "claude",
        pane_dead: bool = False,
    ) -> None:
        self.index = index
        self.name = name
        self.pane_current_command = command
        self.pane_dead = pane_dead


class _FakeTmux:
    """Deterministic in-memory tmux stand-in for the helper's surface."""

    def __init__(self, windows: list[_FakeWindow] | None = None) -> None:
        self.windows: list[_FakeWindow] = list(windows or [])
        self.break_calls: list[tuple[str, str, str]] = []
        self.kill_window_calls: list[str] = []
        self.kill_pane_calls: list[str] = []

    def list_windows(self, session: str) -> list[_FakeWindow]:
        return list(self.windows)

    def break_pane(
        self, source: str, target_session: str, window_name: str
    ) -> None:
        self.break_calls.append((source, target_session, window_name))
        next_index = max(
            (w.index for w in self.windows), default=-1
        ) + 1
        self.windows.append(_FakeWindow(next_index, window_name, command="claude"))

    def kill_window(self, target: str) -> None:
        self.kill_window_calls.append(target)
        try:
            idx = int(target.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        self.windows = [w for w in self.windows if w.index != idx]

    def kill_pane(self, target: str) -> None:
        self.kill_pane_calls.append(target)


def _collect_audit() -> tuple[list[dict[str, Any]], Any]:
    """Return (events, callback) suitable for ``audit_emit=``.

    The callback signature matches the helper's contract:
    ``(event_name, status, metadata)``.
    """

    events: list[dict[str, Any]] = []

    def _emit(event_name: str, status: str, metadata: dict[str, Any]) -> None:
        events.append(
            {"event_name": event_name, "status": status, "metadata": metadata}
        )

    return events, _emit


def test_safe_break_unconditional_when_no_existing_window() -> None:
    """No duplicate → helper just breaks the pane, returns True."""
    tmux = _FakeTmux(windows=[])
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is True
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "architect-samblog")
    ]
    assert tmux.kill_window_calls == []
    assert tmux.kill_pane_calls == []
    assert audit_events == []
    assert [w.name for w in tmux.windows] == ["architect-samblog"]


def test_safe_break_skipped_when_live_duplicate_exists() -> None:
    """Live duplicate → helper refuses to break-pane.

    This is the #1631 wipe surface: parking on top of an existing live
    window would silently double the canonical name and the next mount
    would land on the wrong one.
    """
    existing = _FakeWindow(22, "architect-samblog", command="claude")
    tmux = _FakeTmux(windows=[existing])
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is False
    assert tmux.break_calls == []
    # Source pane was killed so it doesn't orphan in the cockpit.
    assert tmux.kill_pane_calls == ["%99"]
    # Storage closet still has exactly one architect-samblog window.
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 1
    # Audit captures the skip with the duplicate's index.
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_name"] == "cockpit.park_skipped_existing"
    assert event["status"] == "warn"
    assert event["metadata"]["live_duplicate_indices"] == [22]
    assert event["metadata"]["window_name"] == "architect-samblog"
    assert event["metadata"]["storage_session"] == "pollypm-storage-closet"
    assert event["metadata"]["subject"] == "architect_samblog"


def test_safe_break_kills_dead_duplicates_then_breaks() -> None:
    """Dead-named duplicate → killed first so break-pane re-occupies."""
    dead = _FakeWindow(7, "architect-samblog", command="bash", pane_dead=True)
    tmux = _FakeTmux(windows=[dead])
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is True
    assert tmux.kill_window_calls == ["pollypm-storage-closet:7"]
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "architect-samblog")
    ]
    # The dead window was killed; the new break-pane added a fresh one
    # at the canonical name.  No duplicate accumulation.
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 1
    assert audit_events == []  # No skip event for the dead-only case.


def test_safe_break_does_not_add_third_when_two_duplicates_exist() -> None:
    """Smoking gun from Sam's 2026-05-19 wipe.

    The storage closet already had two ``architect-samblog`` windows
    (indices 22 and 42) when the user clicked away from PM Chat.  A
    naive break-pane would push the count to three.  The helper must
    refuse and leave the closet exactly as it found it.
    """
    tmux = _FakeTmux(
        windows=[
            _FakeWindow(22, "architect-samblog", command="node"),
            _FakeWindow(42, "architect-samblog", command="node"),
        ]
    )
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%5099",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is False
    assert tmux.break_calls == []
    assert tmux.kill_pane_calls == ["%5099"]
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 2, (
        "Helper must NOT add a third architect-samblog window — that's "
        "the #1631 conversation-wipe surface"
    )
    assert len(audit_events) == 1
    assert audit_events[0]["metadata"]["live_duplicate_indices"] == [22, 42]


def test_safe_break_treats_version_string_command_as_live() -> None:
    """Live-provider definition mirrors the cockpit selector.

    The Claude CLI reports its version string (e.g. ``2.1.144``) as the
    pane command after the first turn.  The helper must treat such
    panes as live so the user's first-turn-completed conversation
    isn't classified as dead and silently duplicated.
    """
    existing = _FakeWindow(22, "architect-samblog", command="2.1.144")
    tmux = _FakeTmux(windows=[existing])
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is False
    assert tmux.break_calls == []
    assert len(audit_events) == 1
    assert audit_events[0]["event_name"] == "cockpit.park_skipped_existing"


def test_safe_break_swallows_list_windows_errors() -> None:
    """A flaky tmux must not block the park path.

    If ``list_windows`` raises, the helper has no signal about
    duplicates — falling back to the bare break-pane keeps the park
    path moving rather than wedging the cockpit.  (The helper's
    conservative default is to break-pane when in doubt; in the worst
    case a duplicate is created but the cockpit stays usable.)
    """

    class _ExplodingTmux(_FakeTmux):
        def list_windows(self, session: str) -> list[_FakeWindow]:
            raise RuntimeError("tmux server unreachable")

    tmux = _ExplodingTmux(windows=[])
    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
    )

    assert broke is True
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "architect-samblog")
    ]


def test_safe_break_audit_callback_is_optional() -> None:
    """Helper must work without an audit callback (low-level callers)."""
    existing = _FakeWindow(22, "pm-operator", command="claude")
    tmux = _FakeTmux(windows=[existing])

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="pm-operator",
    )

    assert broke is False
    assert tmux.break_calls == []
    assert tmux.kill_pane_calls == ["%99"]
