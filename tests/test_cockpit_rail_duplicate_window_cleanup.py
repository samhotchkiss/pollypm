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
