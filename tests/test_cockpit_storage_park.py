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

* Live duplicate in storage → the duplicate is treated as a stale
  ORPHAN (the cockpit pane being parked is by construction the
  user's active mount).  The orphan is killed, ``break-pane`` runs,
  audit fires with ``cockpit.park_killed_orphan``, helper returns
  ``True``.  See #1994 — the original "skip break-pane and kill the
  cockpit pane" policy wiped Sam's live architect chat on every
  rail-navigation and back.
* Dead duplicates → killed before break-pane so the freshly-parked
  pane re-occupies the canonical name.
* No duplicates → unconditional break-pane.

The smoking-gun scenario from Sam's 2026-05-19 wipe (architect-samblog
duplicated at indices 22 and 42 in the closet) is reproduced as
``test_safe_break_kills_all_live_orphans_when_two_duplicates_exist``.
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


def test_safe_break_kills_orphan_when_live_duplicate_exists() -> None:
    """Live duplicate → helper kills the orphan and breaks the pane.

    #1994 — Sam's verbatim bug: ``I was talking with the Sam blog
    architect.  I click into a different area on the rail.  I click
    back to the Sam blog architect, and it's a new goddamn
    conversation.``  The cockpit pane (``%99`` here) is the user's
    active mount — they were just typing into it.  The storage
    window with the same canonical name is a stale orphan from a
    prior session.  Old behaviour: refuse break-pane and kill ``%99``
    (wiping Sam's live chat).  New behaviour: kill the orphan,
    break-pane our active mount into storage so the next mount picks
    up the real conversation.
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

    assert broke is True
    # The orphan was killed BEFORE the break-pane so the canonical
    # name was available.
    assert tmux.kill_window_calls == ["pollypm-storage-closet:22"]
    # The cockpit's live mount was broken into storage under the
    # canonical name — the user's conversation is preserved.
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "architect-samblog")
    ]
    # The helper must NEVER kill the source pane in the live-duplicate
    # branch — that was the #1994 regression surface.
    assert tmux.kill_pane_calls == []
    # Storage closet still has exactly one architect-samblog window
    # (the freshly broken-in cockpit pane).
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 1
    # Audit captures the orphan kill with the duplicate's index.
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_name"] == "cockpit.park_killed_orphan"
    assert event["status"] == "warn"
    assert event["metadata"]["live_duplicate_indices"] == [22]
    assert event["metadata"]["window_name"] == "architect-samblog"
    assert event["metadata"]["storage_session"] == "pollypm-storage-closet"
    assert event["metadata"]["subject"] == "architect_samblog"
    assert event["metadata"]["reason"] == (
        "killed_orphan_to_preserve_active_mount"
    )


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


def test_safe_break_kills_all_live_orphans_when_two_duplicates_exist() -> None:
    """Two live orphans → both killed before break-pane.

    The storage closet already had two ``architect-samblog`` windows
    (indices 22 and 42) when the user clicked away from PM Chat.
    Both are stale orphans (the user can only be conversing with one
    pane at a time, and that pane is ``%5099`` — the cockpit mount).
    The helper must kill both orphans and break-pane the cockpit
    mount into storage under the canonical name so the next mount
    finds exactly one ``architect-samblog`` window holding the
    user's actual conversation.

    Pre-#1994 behaviour: refuse to break and kill ``%5099``,
    wiping the user's live conversation.
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

    assert broke is True
    # Both orphans killed before the break-pane.
    assert tmux.kill_window_calls == [
        "pollypm-storage-closet:22",
        "pollypm-storage-closet:42",
    ]
    # The cockpit mount was broken in under the canonical name.
    assert tmux.break_calls == [
        ("%5099", "pollypm-storage-closet", "architect-samblog")
    ]
    # The user's source pane was NEVER killed — the conversation is
    # preserved inside the broken-out window.
    assert tmux.kill_pane_calls == []
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 1, (
        "After the park, storage must have exactly one architect-"
        "samblog window — the one holding the user's actual "
        "conversation (the broken-in cockpit mount)"
    )
    assert len(audit_events) == 1
    assert audit_events[0]["event_name"] == "cockpit.park_killed_orphan"
    assert audit_events[0]["metadata"]["live_duplicate_indices"] == [22, 42]


def test_safe_break_treats_version_string_command_as_live() -> None:
    """Live-provider definition mirrors the cockpit selector.

    The Claude CLI reports its version string (e.g. ``2.1.144``) as the
    pane command after the first turn.  The helper must treat such
    panes as live so a first-turn-completed orphan is recognised and
    killed (rather than mistaken for a dead window and only re-occupied
    on top of).
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

    assert broke is True
    assert tmux.kill_window_calls == ["pollypm-storage-closet:22"]
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "architect-samblog")
    ]
    assert len(audit_events) == 1
    assert audit_events[0]["event_name"] == "cockpit.park_killed_orphan"


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
    """Helper must work without an audit callback (low-level callers).

    With #1994's policy reversal, a missing audit callback still
    causes the orphan to be killed and break-pane to proceed; the
    audit emit is best-effort and silent without a callback.
    """
    existing = _FakeWindow(22, "pm-operator", command="claude")
    tmux = _FakeTmux(windows=[existing])

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%99",
        storage_session="pollypm-storage-closet",
        window_name="pm-operator",
    )

    assert broke is True
    assert tmux.kill_window_calls == ["pollypm-storage-closet:22"]
    assert tmux.break_calls == [
        ("%99", "pollypm-storage-closet", "pm-operator")
    ]
    assert tmux.kill_pane_calls == []


def test_safe_break_preserves_active_mount_across_rail_navigation() -> None:
    """#1994 regression: rail nav must NOT wipe the user's chat.

    Reproduces Sam's verbatim bug (2026-05-20):

        I was talking with the Sam blog architect.  I click into a
        different area on the rail.  I click back to the Sam blog
        architect, and it's a new goddamn conversation.

    Pre-condition: the storage closet has a stale ``architect-samblog``
    orphan from a prior cockpit lifetime (a common state — the #1631
    history is exactly this).  The user has been actively typing into
    the cockpit's mounted architect pane (``%9001``).  Rail navigation
    triggers a park of ``%9001`` under the canonical name.

    Old behaviour: helper saw the storage orphan, killed ``%9001``
    (wiping the user's chat), left the orphan in place; the next
    mount surfaced the orphan as if it were the user's conversation.

    New behaviour: helper kills the orphan, breaks ``%9001`` into
    storage under the canonical name; the next mount surfaces the
    user's actual conversation.
    """
    orphan = _FakeWindow(17, "architect-samblog", command="claude")
    tmux = _FakeTmux(windows=[orphan])
    audit_events, audit_emit = _collect_audit()

    broke = safe_break_pane_to_storage(
        tmux,
        source_pane_id="%9001",
        storage_session="pollypm-storage-closet",
        window_name="architect-samblog",
        audit_emit=audit_emit,
        subject="architect_samblog",
    )

    assert broke is True
    # The orphan was reaped.
    assert "pollypm-storage-closet:17" in tmux.kill_window_calls
    # The user's active mount was preserved by being broken into storage.
    assert tmux.break_calls == [
        ("%9001", "pollypm-storage-closet", "architect-samblog")
    ]
    # CRITICAL: the helper must NEVER kill the active mount in the
    # rail-navigation case — that's the conversation-wipe surface.
    assert tmux.kill_pane_calls == [], (
        "safe_break_pane_to_storage killed the active mount — #1994 "
        "regression"
    )
    # Exactly one architect-samblog window remains in storage and it
    # is the one we just broke in (its index is one higher than the
    # killed orphan in the fake tmux).
    same_name = [w for w in tmux.windows if w.name == "architect-samblog"]
    assert len(same_name) == 1
    assert audit_events[0]["event_name"] == "cockpit.park_killed_orphan"
