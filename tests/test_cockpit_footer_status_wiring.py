"""Integration tests for the cockpit footer wiring (refs #1988, #2027).

PR #2014 shipped :func:`pollypm.cockpit_footer_status.render_footer_status`
as a pure formatter with leaf-level coverage. The wiring PR
(:func:`PollyCockpitApp._update_hint` -> ``render_footer_status``) lives
in ``src/pollypm/cockpit_ui.py``; these tests lock in:

1. The wired call site reads from the precomputed
   :class:`FooterStateSnapshot` and writes the rendered string to
   ``self.hint`` without doing supervisor / inbox / heartbeat resolution
   on the UI thread (#2027 perf invariant).
2. Narrow-width renders still satisfy the helper's "no overflow"
   invariant — the wire-up must not paint a string wider than the
   ``hint`` widget's measured width.
3. Snapshot resolution happens off-thread in
   :meth:`PollyCockpitApp._resolve_footer_state` and feeds the same
   inputs as the legacy in-line builder.

All tests bypass ``__init__`` (``PollyCockpitApp.__new__``) so they
can stub the router / hint widget without spinning up Textual.
"""

from __future__ import annotations

import re

from pollypm.cockpit_ui import FooterStateSnapshot, PollyCockpitApp


_MARKUP_RE = re.compile(r"\[/?[^\]]+\]")


def _plain(markup: str) -> str:
    """Strip Rich markup tags so length assertions reflect rendered width."""
    return _MARKUP_RE.sub("", markup)


class _StubHint:
    """Minimal stand-in for the cockpit's ``Static`` hint widget."""

    def __init__(self, width: int = 120) -> None:
        self.size = type("Size", (), {"width": width})()
        self.last_text: str | None = None

    def update(self, text: str) -> None:
        self.last_text = text


class _StubConfig:
    def __init__(self, projects: dict, sessions: dict) -> None:
        self.projects = projects
        self.sessions = sessions


class _StubStore:
    def last_heartbeat_at(self) -> str | None:
        return None  # fresh heartbeat → no alert chunk


class _StubSupervisor:
    def __init__(self, projects: dict, sessions: dict) -> None:
        self.config = _StubConfig(projects, sessions)
        self.store = _StubStore()


class _StubRouter:
    def __init__(self, supervisor: _StubSupervisor) -> None:
        self._supervisor = supervisor

    def _load_supervisor(self, fresh: bool = False):  # noqa: ARG002
        return self._supervisor


def _build_app(
    *,
    projects: dict | None = None,
    sessions: dict | None = None,
    inbox_count: int = 0,
    hint_width: int = 120,
    monkeypatch=None,
    seed_footer_state: bool = True,
) -> tuple[PollyCockpitApp, _StubHint]:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    supervisor = _StubSupervisor(projects or {}, sessions or {})
    app.router = _StubRouter(supervisor)  # type: ignore[assignment]
    app._route_status_hint = None  # type: ignore[attr-defined]
    app._footer_state = None  # type: ignore[attr-defined]
    app._right_pane_has_live_session = lambda: False  # type: ignore[method-assign]
    hint = _StubHint(width=hint_width)
    app.hint = hint  # type: ignore[assignment]

    # Stub the cached inbox fanout so the test doesn't need a real DB.
    fake_items = [object()] * inbox_count
    if monkeypatch is not None:
        monkeypatch.setattr(
            "pollypm.cockpit_inbox.pm_inbox_awaits_user_list",
            lambda _config: list(fake_items),
        )

    # Precompute the footer snapshot so ``_update_hint`` (which is now
    # a pure formatter — see #2027) has something to render. In
    # production this is populated off-thread by
    # ``_refresh_rows_worker`` before ``_apply_built_items`` ->
    # ``_update_hint`` runs.
    if seed_footer_state:
        app._footer_state = app._resolve_footer_state()  # type: ignore[attr-defined]

    return app, hint


def test_update_hint_renders_unified_status_with_supervisor_counts(monkeypatch) -> None:
    """``_update_hint`` reads the precomputed footer snapshot and writes
    the unified status string into ``self.hint``.
    """
    projects = {f"p{i}": object() for i in range(5)}
    sessions = {f"s{i}": object() for i in range(7)}
    app, hint = _build_app(
        projects=projects,
        sessions=sessions,
        inbox_count=3,
        hint_width=120,
        monkeypatch=monkeypatch,
    )

    app._update_hint()

    assert hint.last_text is not None, "footer was never updated"
    plain = _plain(hint.last_text)
    # Counts must surface as labeled chunks at this comfortable width.
    assert "5 projects" in plain, plain
    assert "7 agents" in plain, plain
    assert "3 inbox" in plain, plain


def test_update_hint_narrow_width_respects_helper_invariant(monkeypatch) -> None:
    """The helper guarantees the rendered text fits inside ``width`` —
    the wire-up must honor that invariant by passing the live hint
    widget's width through. Use a narrow 30-col rail-style budget so
    the helper has to drop labels / truncate.
    """
    projects = {f"p{i}": object() for i in range(99)}
    sessions = {f"s{i}": object() for i in range(99)}
    app, hint = _build_app(
        projects=projects,
        sessions=sessions,
        inbox_count=42,
        hint_width=30,
        monkeypatch=monkeypatch,
    )

    app._update_hint()

    assert hint.last_text is not None
    rendered_width = len(_plain(hint.last_text))
    assert rendered_width <= 30, (
        f"footer overflowed 30-col rail: {rendered_width} chars "
        f"-> {hint.last_text!r}"
    )


def test_update_hint_falls_back_to_legacy_on_helper_failure(monkeypatch) -> None:
    """If the unified path raises (helper bug, transient store error,
    anything), the footer must NEVER crash — fall through to the
    pre-#1988 inline string so the cockpit footer keeps rendering.
    """
    app, hint = _build_app(
        projects={"only": object()},
        sessions={"one": object()},
        inbox_count=0,
        hint_width=120,
        monkeypatch=monkeypatch,
    )

    def _boom(**_kwargs: object) -> str:
        raise RuntimeError("simulated helper regression")

    monkeypatch.setattr("pollypm.cockpit_ui.render_footer_status", _boom)

    app._update_hint()

    # Legacy default hint for the no-live-session branch.
    assert hint.last_text == "j/k ↵open · ? help · q quit", (
        f"backstop did not produce the legacy hint: {hint.last_text!r}"
    )


def test_update_hint_renders_legacy_when_snapshot_not_yet_populated(
    monkeypatch,
) -> None:
    """First paint, before the off-thread rail worker has populated
    ``_footer_state``, must render the legacy hint instead of running
    the supervisor/inbox/heartbeat resolution on the UI thread (#2027).
    """
    app, hint = _build_app(
        projects={"only": object()},
        sessions={"one": object()},
        inbox_count=0,
        hint_width=120,
        monkeypatch=monkeypatch,
        seed_footer_state=False,
    )
    assert app._footer_state is None  # precondition

    app._update_hint()

    assert hint.last_text == "j/k ↵open · ? help · q quit", (
        f"first-paint fallback did not produce the legacy hint: {hint.last_text!r}"
    )


def test_update_hint_route_status_hint_still_wins() -> None:
    """The transient per-route override (e.g. ``Press n to launch``)
    must continue to short-circuit the unified status bar so route
    feedback isn't overwritten by the workspace-wide counts.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app._route_status_hint = "Launching worker for demo..."  # type: ignore[attr-defined]
    hint = _StubHint(width=120)
    app.hint = hint  # type: ignore[assignment]

    app._update_hint()

    assert hint.last_text == "Launching worker for demo..."


def test_update_hint_does_not_call_pm_inbox_awaits_user_list_on_hot_path(
    monkeypatch,
) -> None:
    """#2027 regression — ``_update_hint`` runs on the rail repaint
    path (``_apply_built_items`` -> ``_update_hint``) and MUST NOT
    synchronously call :func:`pm_inbox_awaits_user_list` on the UI
    thread. The expensive inbox fanout happens off-thread inside
    :meth:`_resolve_footer_state`; ``_update_hint`` is a pure
    formatter over the precomputed :class:`FooterStateSnapshot`.

    A click burst (e.g. j/k spam) can re-enter ``_update_hint``
    dozens of times per second; allowing the fanout on the hot path
    blocks every keystroke for the duration of the supervisor load
    + inbox sweep + heartbeat probe, which is the same surface
    PR #2027 was already trying to keep cold.
    """
    inbox_call_count = 0

    def _tracked_inbox(_config):
        nonlocal inbox_call_count
        inbox_call_count += 1
        return []

    # Patch the symbol at its source so any ``from pollypm.cockpit_inbox
    # import pm_inbox_awaits_user_list`` re-import inside ``_update_hint``
    # also routes through the tracker.
    monkeypatch.setattr(
        "pollypm.cockpit_inbox.pm_inbox_awaits_user_list",
        _tracked_inbox,
    )

    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app._route_status_hint = None  # type: ignore[attr-defined]
    app._right_pane_has_live_session = lambda: False  # type: ignore[method-assign]
    # Seed a precomputed snapshot — this is the contract: by the time
    # ``_update_hint`` is on the UI thread, ``_footer_state`` is already
    # populated (off-thread, by ``_refresh_rows_worker``).
    app._footer_state = FooterStateSnapshot(  # type: ignore[attr-defined]
        project_count=4,
        agent_count=2,
        inbox_count=5,
        alert=None,
    )
    hint = _StubHint(width=120)
    app.hint = hint  # type: ignore[assignment]

    # Simulate a click burst — many ``_update_hint`` re-entries per
    # the rail repaint path. The fanout MUST stay at 0 calls.
    for _ in range(25):
        app._update_hint()

    assert inbox_call_count == 0, (
        f"_update_hint ran pm_inbox_awaits_user_list on the hot path "
        f"({inbox_call_count} call(s)); footer inputs must be precomputed "
        f"off-thread per #2027."
    )
    # Sanity check: the footer was actually rendered, not skipped.
    assert hint.last_text is not None
    plain = _plain(hint.last_text)
    assert "5 inbox" in plain or "5" in plain, plain


def test_resolve_footer_state_returns_snapshot_with_expected_counts(
    monkeypatch,
) -> None:
    """The off-thread resolver produces a :class:`FooterStateSnapshot`
    whose fields match what ``_update_hint`` would have computed
    inline pre-#2027. Locks in that the move-to-snapshot didn't
    change the semantic of the four footer inputs.
    """
    projects = {f"p{i}": object() for i in range(6)}
    sessions = {f"s{i}": object() for i in range(4)}
    app, _hint = _build_app(
        projects=projects,
        sessions=sessions,
        inbox_count=11,
        hint_width=120,
        monkeypatch=monkeypatch,
        seed_footer_state=False,
    )

    snapshot = app._resolve_footer_state()

    assert isinstance(snapshot, FooterStateSnapshot)
    assert snapshot.project_count == 6
    assert snapshot.agent_count == 4
    assert snapshot.inbox_count == 11
    # Stub store reports no heartbeat → no alert chunk.
    assert snapshot.alert is None
