"""#1562 — ``_cleanup_duplicate_windows`` conservative-kill regression.

Top-level Polly conversation was vanishing on rail switch
(click Polly → conversation → click inbox → click Polly → fresh
standing-by prompt). One plausible cause was
``_cleanup_duplicate_windows`` keeping the lowest-index window per
name and killing the rest unconditionally — so a freshly-parked
operator pane at a higher index than a stale empty pm-operator
window could itself be the one that got killed.

These tests pin the new contract:
- Only ``pane_dead=True`` duplicates get killed.
- Multiple live duplicates: don't pick, log warn, bail.
- Non-duplicates: untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from pollypm.cockpit_rail import CockpitRouter
from pollypm.tmux.client import TmuxWindow


def _window(
    *, index: int, name: str, pane_dead: bool, session: str = "pm-storage-closet",
) -> TmuxWindow:
    return TmuxWindow(
        session=session,
        index=index,
        name=name,
        active=False,
        pane_id=f"%{index}",
        pane_current_command="zsh",
        pane_current_path="/tmp",
        pane_dead=pane_dead,
        pane_pid=10_000 + index,
    )


class _FakeTmux:
    def __init__(self, windows: list[TmuxWindow]) -> None:
        self._windows = list(windows)
        self.killed: list[str] = []

    def list_windows(self, target: str) -> list[TmuxWindow]:
        return list(self._windows)

    def kill_window(self, target: str) -> None:
        self.killed.append(target)
        # Mirror tmux: drop the window from state so subsequent reads
        # are consistent.
        _, _, index_str = target.partition(":")
        try:
            index = int(index_str)
        except ValueError:
            return
        self._windows = [w for w in self._windows if w.index != index]


def _bare_router(tmux: _FakeTmux) -> CockpitRouter:
    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = tmux  # type: ignore[attr-defined]
    return router


def test_cleanup_kills_dead_duplicate_only_keeps_live() -> None:
    """A stale dead pm-operator must not take down the live one."""
    tmux = _FakeTmux([
        _window(index=0, name="pm-operator", pane_dead=True),
        _window(index=1, name="pm-operator", pane_dead=False),
        _window(index=2, name="pm-russell", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    assert tmux.killed == ["pm-storage-closet:0"]
    surviving = {w.index: w.name for w in tmux._windows}
    assert surviving == {1: "pm-operator", 2: "pm-russell"}


def test_cleanup_skips_when_both_duplicates_have_live_panes() -> None:
    """Two live duplicates of the same name: don't pick — bail."""
    tmux = _FakeTmux([
        _window(index=0, name="pm-operator", pane_dead=False),
        _window(index=1, name="pm-operator", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    assert tmux.killed == []
    assert {w.index for w in tmux._windows} == {0, 1}


def test_cleanup_kills_multiple_dead_duplicates_when_one_live_survivor() -> None:
    """Several stale dead copies plus one live winner: kill the dead."""
    tmux = _FakeTmux([
        _window(index=0, name="pm-operator", pane_dead=True),
        _window(index=1, name="pm-operator", pane_dead=True),
        _window(index=2, name="pm-operator", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    assert tmux.killed == ["pm-storage-closet:0", "pm-storage-closet:1"]
    surviving = {w.index for w in tmux._windows}
    assert surviving == {2}


def test_cleanup_reaps_live_advisor_duplicate() -> None:
    """#1809 — advisor windows are reapable when duplicated.

    The conservative pane-dead-only behaviour preserves operator /
    architect / worker conversations, but advisor sessions carry no
    user-facing conversational state — the persona is loaded from
    ``profiles/advisor.md`` on every launch. When two ``advisor-media``
    windows landed in the storage closet (the #1809 reproduction), the
    plain ``skipped_live_duplicates`` warn left the duplicate idle
    forever; the next advisor.tick couldn't reach it because both panes
    sat at the Codex placeholder. The advisor-specific reaper kills the
    higher-index duplicate so the cadence has a single, primable window
    to address.
    """
    tmux = _FakeTmux([
        _window(index=31, name="advisor-media", pane_dead=False),
        _window(index=32, name="advisor-media", pane_dead=False),
        _window(index=33, name="worker-media", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    # The higher-index duplicate was killed; the canonical low-index
    # advisor window survived. Worker windows are untouched.
    assert tmux.killed == ["pm-storage-closet:32"]
    surviving = {w.index: w.name for w in tmux._windows}
    assert surviving == {31: "advisor-media", 33: "worker-media"}


def test_cleanup_preserves_live_pm_operator_duplicates() -> None:
    """Worker/operator/architect duplicates must still bail conservatively.

    The #1809 advisor-specific reaper is gated on the ``advisor-``
    window-name prefix. This guard ensures we didn't accidentally
    broaden it to a generic kill-the-higher-index policy that would
    take out the operator's conversation pane (#1562).
    """
    tmux = _FakeTmux([
        _window(index=0, name="pm-operator", pane_dead=False),
        _window(index=1, name="pm-operator", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    assert tmux.killed == []
    assert {w.index for w in tmux._windows} == {0, 1}


def test_cleanup_no_op_when_no_duplicates() -> None:
    tmux = _FakeTmux([
        _window(index=0, name="pm-operator", pane_dead=False),
        _window(index=1, name="pm-russell", pane_dead=False),
    ])
    router = _bare_router(tmux)

    router._cleanup_duplicate_windows("pm-storage-closet")

    assert tmux.killed == []


def test_cleanup_tolerates_list_windows_failure() -> None:
    class _BrokenTmux:
        def list_windows(self, target: str) -> list[TmuxWindow]:
            raise RuntimeError("tmux query failed")

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _BrokenTmux()  # type: ignore[attr-defined]

    # Must not raise.
    router._cleanup_duplicate_windows("pm-storage-closet")


def test_emit_cockpit_audit_lands_in_workspace_central_tail(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1593 — emits must land somewhere a `grep` can find them.

    ``audit.log.emit`` skips the central-tail write when ``project`` is
    falsy. ``_emit_cockpit_audit`` does not pass ``project_path``, so an
    empty ``project`` silently drops the event. The fix is to use
    ``"_workspace"`` (matching ``rail_daemon_supervisor`` /
    ``rail_daemon_reaper``). This pins that landing path so the next
    time someone "simplifies" the project key, the
    overnight-protocol watch stays observable.
    """
    audit_home = tmp_path / "audit"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    router = CockpitRouter.__new__(CockpitRouter)
    router._emit_cockpit_audit(
        event_name="cockpit.session_respawned",
        subject="pm-operator",
        status="warn",
        metadata={"reason": "test"},
    )

    workspace_log = audit_home / "_workspace.jsonl"
    assert workspace_log.exists(), (
        f"cockpit audit emit must land in central tail; "
        f"contents of {audit_home}: "
        f"{[p.name for p in audit_home.iterdir()] if audit_home.exists() else 'missing'}"
    )
    lines = [
        json.loads(line)
        for line in workspace_log.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    record = lines[0]
    assert record["event"] == "cockpit.session_respawned"
    assert record["project"] == "_workspace"
    assert record["subject"] == "pm-operator"
    assert record["status"] == "warn"
    assert record["actor"] == "cockpit-rail"
    assert record["metadata"] == {"reason": "test"}
