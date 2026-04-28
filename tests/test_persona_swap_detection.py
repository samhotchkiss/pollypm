"""Tests for persona-swap detection added 2026-04-16.

Context: during an overnight E2E test, the ``pm-operator`` tmux window
was observed running Russell's (reviewer) control prompt. Root cause in
the recovery/bootstrap threading path is untraced. These tests cover the
two fail-loud defenses that were added in response:

1. A strict assertion in ``_prepare_initial_input`` that refuses to
   write or send a kickoff when the ``(launch, target)`` tuple looks
   crossed (both the supervisor path and the session_services path).
2. A verify-after-kickoff backstop that re-captures the pane and
   re-sends the correct prompt when a wrong-persona marker is detected.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor, _ROLE_PERSONA_MARKER


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
            "reviewer": SessionConfig(
                name="reviewer",
                role="reviewer",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-reviewer",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


# ---------------------------------------------------------------------------
# #1 — Assertion inside _prepare_initial_input (supervisor path)
# ---------------------------------------------------------------------------


def test_prepare_initial_input_raises_for_unknown_session(tmp_path: Path) -> None:
    """If ``session_name`` doesn't resolve to any launch, raise loudly."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    with pytest.raises(RuntimeError, match="persona_swap_detected"):
        supervisor._prepare_initial_input("no-such-session", "some prompt text")


def test_prepare_initial_input_raises_when_window_mismatches(
    monkeypatch, tmp_path: Path,
) -> None:
    """Crossed (launch, target) tuple: launch.window_name != expected window."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    # Fake the launch planner to return a launch whose window_name is
    # the *reviewer* window under the operator session name — that's
    # exactly the kind of cross we're trying to catch.
    real_launch = supervisor.launch_by_session("operator")
    bad_launch = replace(real_launch, window_name="pm-reviewer")
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda name: bad_launch,
    )

    with pytest.raises(RuntimeError, match="persona_swap_detected"):
        supervisor._prepare_initial_input("operator", "kickoff")


def test_prepare_initial_input_raises_when_name_mismatches(
    monkeypatch, tmp_path: Path,
) -> None:
    """Planner returned a launch for a different session than requested."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    reviewer_launch = supervisor.launch_by_session("reviewer")
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda name: reviewer_launch,
    )

    with pytest.raises(RuntimeError, match="persona_swap_detected"):
        supervisor._prepare_initial_input("operator", "kickoff")


def test_prepare_initial_input_records_event_on_mismatch(
    monkeypatch, tmp_path: Path,
) -> None:
    """On detected swap, a ``persona_swap_detected`` event is recorded."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    real_launch = supervisor.launch_by_session("operator")
    bad_launch = replace(real_launch, window_name="pm-reviewer")
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda name: bad_launch,
    )

    with pytest.raises(RuntimeError):
        supervisor._prepare_initial_input("operator", "kickoff")

    # #349: persona-swap events now land in the unified ``messages``
    # table via the Store. Query through ``_msg_store`` directly.
    rows = supervisor.msg_store.query_messages(
        type="event",
        scope="operator",
        limit=10,
    )
    matches = [r for r in rows if r.get("subject") == "persona_swap_detected"]
    assert len(matches) == 1
    message = (matches[0].get("payload") or {}).get("message") or ""
    assert "pm-operator" in message
    assert "pm-reviewer" in message


def test_prepare_initial_input_happy_path_returns_prompt(tmp_path: Path) -> None:
    """When everything matches, _prepare_initial_input returns normally."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    # Short prompt — returned verbatim.
    result = supervisor._prepare_initial_input("operator", "short prompt")
    assert result == "short prompt"

    # Long prompt — written to disk, return a reference string.
    long_prompt = "x" * 500
    result = supervisor._prepare_initial_input("operator", long_prompt)
    assert "operator.md" in result


# ---------------------------------------------------------------------------
# #1 — Assertion inside session_services _prepare_initial_input
# ---------------------------------------------------------------------------


def test_session_service_prepare_raises_on_window_mismatch(tmp_path: Path) -> None:
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    # The supervisor constructor sets up the state DB directory — reuse
    # it so we can create a session service directly.
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)

    with pytest.raises(RuntimeError, match="persona_swap_detected"):
        service._prepare_initial_input(
            "operator",
            "kickoff",
            expected_window="pm-reviewer",  # wrong window for operator
            session_role="operator-pm",
        )


def test_session_service_prepare_happy_path(tmp_path: Path) -> None:
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)

    result = service._prepare_initial_input(
        "operator",
        "short",
        expected_window="pm-operator",
        session_role="operator-pm",
    )
    assert result == "short"


def test_session_service_target_window_helper_refuses_crossed_pane(
    tmp_path: Path,
) -> None:
    """#932 — session-service kickoff path refuses crossed (wname, target).

    The session-service ``create()`` path runs the same target-window
    crossing guard the supervisor does. Verify directly via the
    module-level ``_target_window_matches_expected`` helper that a pane
    living in a different window is rejected, while a pane in the
    expected window is accepted.
    """
    from pollypm.session_services.tmux import _target_window_matches_expected

    class _FakeTmux:
        def __init__(self, window_name: str) -> None:
            self._window_name = window_name

        def list_panes(self, target: str) -> list[object]:
            return [type("P", (), {"window_name": self._window_name})()]

    # Crossed: pane belongs to pm-operator but expected window is pm-heartbeat.
    assert _target_window_matches_expected(
        _FakeTmux("pm-operator"), "pm-heartbeat", "%any",
    ) is False

    # Match: pane belongs to pm-operator and expected window is pm-operator.
    assert _target_window_matches_expected(
        _FakeTmux("pm-operator"), "pm-operator", "%any",
    ) is True

    # Probe failure (raise): conservative pass-through (returns True).
    class _RaisingTmux:
        def list_panes(self, target: str) -> list[object]:
            raise RuntimeError("transient tmux error")

    assert _target_window_matches_expected(
        _RaisingTmux(), "pm-operator", "%any",
    ) is True

    # No expected window: no-op (returns True).
    assert _target_window_matches_expected(
        _FakeTmux("pm-operator"), None, "%any",
    ) is True


def test_session_service_prepare_skips_check_for_worker(tmp_path: Path) -> None:
    """Worker sessions are transient and not in static config; the
    session-service assertion must no-op for them."""
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)

    # Worker session name that's NOT in the static config — should not raise.
    result = service._prepare_initial_input(
        "worker-task-42",
        "short",
        expected_window="worker-task-42",
        session_role="worker",
    )
    assert result == "short"


# ---------------------------------------------------------------------------
# #2 — Verify-after-kickoff
# ---------------------------------------------------------------------------


def _patch_tmux(monkeypatch, supervisor: Supervisor, pane_text: str, sends: list):
    """Install fake tmux capture/send on the supervisor's session_service."""
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=50: pane_text,
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sends.append((target, text)),
    )


def test_verify_after_kickoff_noop_when_marker_matches(
    monkeypatch, tmp_path: Path,
) -> None:
    """Expected marker present, no unexpected markers — do nothing."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launch = supervisor.launch_by_session("operator")
    assert launch.session.role == "operator-pm"

    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text="I am Polly, ready.",
        sends=sends,
    )
    # Skip the 5 s wait.
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(launch, "pollypm-storage-closet:pm-operator")
    # Thread is daemon — join briefly.
    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    # Happy path: no resend.
    assert sends == []

    # #349: events live in the unified ``messages`` table.
    events = supervisor.msg_store.query_messages(
        type="event",
        scope="operator",
        limit=20,
    )
    assert not any(
        event.get("subject") == "persona_swap_verified" for event in events
    )


def test_verify_after_kickoff_resends_on_wrong_persona(
    monkeypatch, tmp_path: Path,
) -> None:
    """Pane shows Russell in the Polly window — record + resend."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launch = supervisor.launch_by_session("operator")
    # Ensure our launch has a non-empty initial_input so the resend
    # branch has something to work with.
    launch_with_input = replace(launch, initial_input="polly kickoff text")
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda name: launch_with_input,
    )

    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text="I am Russell, ready for review.",
        sends=sends,
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(
        launch_with_input, "pollypm-storage-closet:pm-operator",
    )
    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    # We expect one recovery resend attempt.
    assert len(sends) == 1
    assert sends[0][0] == "pollypm-storage-closet:pm-operator"

    # And a persona_swap_verified event recorded.
    # #349: events land in the unified ``messages`` table via the Store.
    events = supervisor.msg_store.query_messages(
        type="event",
        scope="operator",
        limit=20,
    )
    matches = [
        event for event in events
        if event.get("subject") == "persona_swap_verified"
    ]
    assert len(matches) == 1
    message_text = (matches[0].get("payload") or {}).get("message") or ""
    assert "Russell" in message_text


def test_verify_after_kickoff_skips_for_worker_role(
    monkeypatch, tmp_path: Path,
) -> None:
    """Worker role has no persona marker — verification must short-circuit."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launch = supervisor.launch_by_session("operator")
    worker_launch = replace(
        launch,
        session=replace(launch.session, role="worker"),
    )

    captures: list[str] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=50: captures.append(target) or "",
    )

    supervisor._schedule_persona_verify(worker_launch, "some:target")

    # No thread was spawned — capture should never be called.
    import threading
    import time
    time.sleep(0.05)
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=0.1)
    assert captures == []


def test_role_persona_marker_covers_expected_roles() -> None:
    """Sanity: the role→marker map covers every control role with a persona."""
    assert _ROLE_PERSONA_MARKER["operator-pm"] == "Polly"
    assert _ROLE_PERSONA_MARKER["reviewer"] == "Russell"
    assert _ROLE_PERSONA_MARKER["heartbeat-supervisor"] == "Heartbeat"
    # Worker and triage intentionally absent — no stable persona.
    assert "worker" not in _ROLE_PERSONA_MARKER
    assert "triage" not in _ROLE_PERSONA_MARKER


# ---------------------------------------------------------------------------
# #933 — Third send-path guard on the persona-verify resend
# ---------------------------------------------------------------------------
#
# #931 (banner) and #932 (target-window) plugged the foreground kickoff
# paths, but Sam reported the cockpit operator pane STILL receiving a
# heartbeat bootstrap as its FIRST kickoff message after both fixes
# landed. The third send path was the background persona-verify resend
# in ``_schedule_persona_verify`` — it captured a ``target`` 5 s before
# the resend and then called ``send_keys(target, kickoff)`` without
# re-checking that ``target`` still resolved to a pane in the launch's
# window or that the pane wasn't already bootstrapped for another role.
# When tmux recycled the original heartbeat pane id for a freshly-
# spawned operator pane (cockpit ``Polly · chat`` flow), the resend
# silently injected ``Read .../heartbeat.md`` into the operator pane.
#
# The fix mirrors #931/#932: re-run ``_target_window_matches_launch``
# and ``_pane_already_bootstrapped_as_other_role`` immediately before
# the resend send_keys so all three send sites share one defense.


def _make_pane(window_name: str, pane_id: str = "%fake") -> object:
    """Build a minimal pane stub that exposes window_name + pane_id.

    Mirrors the helper in ``tests.test_supervisor`` — duplicated here to
    avoid a cross-test-module import.
    """
    return type(
        "Pane", (),
        {
            "window_name": window_name,
            "pane_id": pane_id,
            "pane_left": 0,
            "pane_current_command": "claude",
            "pane_dead": False,
        },
    )()


def test_persona_verify_resend_refused_when_target_in_other_window(
    monkeypatch, tmp_path: Path,
) -> None:
    """#933 — persona-verify resend must not cross windows.

    Reproduces the live failure: the heartbeat persona-verify thread
    captured ``target=%5`` (heartbeat pane in storage closet) at kickoff
    time. By the time the 5 s wait expires, that pane id has been
    recycled by tmux for the operator pane in the cockpit window
    (``Polly · chat`` spawned a fresh operator). The persona-verify
    thread then sees an unexpected (operator) marker in pane and tries
    to resend the heartbeat kickoff into ``%5`` — which is now the
    operator pane.

    The new guard resolves ``%5`` to its current window
    (``pm-operator-pm``) and refuses the resend because
    ``launch.window_name == "pm-heartbeat"``. No heartbeat bootstrap
    text reaches the operator pane.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    heartbeat_launch = supervisor.launch_by_session("heartbeat")
    heartbeat_launch = replace(
        heartbeat_launch, initial_input="heartbeat kickoff text",
    )
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda _name: heartbeat_launch,
    )

    # The pane currently shows the operator persona marker — the
    # verify thread's "expected (heartbeat) absent + unexpected (operator)
    # present" branch is what would have triggered the unguarded resend.
    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text="I am Polly, ready.",
        sends=sends,
    )
    # tmux now reports that the captured target pane lives in the
    # operator window — the (launch, target) tuple is crossed.
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-operator", pane_id="%5")],
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(heartbeat_launch, "%5")

    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    assert sends == [], (
        "heartbeat verify-resend must NOT land in a pane that now lives "
        "in the operator window — that's the #933 third-site bug"
    )


def test_persona_verify_resend_refused_when_pane_has_other_role_banner(
    monkeypatch, tmp_path: Path,
) -> None:
    """#933 — persona-verify resend must not stack on another role's banner.

    Layered defense: even if the target window check passes (target is
    still in the launch's window per tmux), the pane may carry another
    role's banner from a prior bootstrap. Refuse there too — same
    semantics as the #931 banner guard wired into the foreground
    send path.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    heartbeat_launch = supervisor.launch_by_session("heartbeat")
    heartbeat_launch = replace(
        heartbeat_launch, initial_input="heartbeat kickoff text",
    )
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda _name: heartbeat_launch,
    )

    # Pane reports the heartbeat window (target-window guard passes)
    # but carries an operator-pm CANONICAL ROLE banner — banner guard
    # must refuse.
    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text=(
            "======================================================================\n"
            "CANONICAL ROLE: operator-pm\n"
            "SESSION NAME:   operator\n"
            "======================================================================\n"
            "I am Polly, ready.\n"
        ),
        sends=sends,
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-heartbeat", pane_id="%hb")],
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(heartbeat_launch, "%hb")

    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    assert sends == [], (
        "heartbeat verify-resend must NOT stack on top of an existing "
        "operator-pm banner — the pane has already been bootstrapped"
    )


def test_persona_verify_resend_proceeds_when_target_window_matches(
    monkeypatch, tmp_path: Path,
) -> None:
    """#933 — the legitimate verify-resend path still works.

    The mainline persona-verify case: pane is in the right window and
    the unexpected marker is genuinely present (no banner stacking).
    The resend must still go through so a real persona-swap recovery
    is not regressed.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    operator_launch = supervisor.launch_by_session("operator")
    operator_launch = replace(
        operator_launch, initial_input="polly kickoff text",
    )
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda _name: operator_launch,
    )

    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text="I am Russell, ready for review.",
        sends=sends,
    )
    # Target is genuinely in the operator window — guard lets it through.
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [_make_pane(window_name="pm-operator", pane_id="%op")],
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(operator_launch, "%op")

    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    assert len(sends) == 1, (
        "verify-resend must still fire on a real persona swap when the "
        "(launch, target) tuple matches"
    )
    assert sends[0][0] == "%op"


def test_persona_verify_resend_heartbeat_target_in_heartbeat_window_proceeds(
    monkeypatch, tmp_path: Path,
) -> None:
    """#933 — heartbeat verify-resend lands cleanly when window matches.

    Confirms the guard isn't biased toward operator panes — every
    role's verify-resend routes back into its own window. The heartbeat
    pane in the storage closet remains a legitimate kickoff target for
    the heartbeat session.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    heartbeat_launch = supervisor.launch_by_session("heartbeat")
    heartbeat_launch = replace(
        heartbeat_launch, initial_input="heartbeat kickoff text",
    )
    monkeypatch.setattr(
        supervisor, "launch_by_session", lambda _name: heartbeat_launch,
    )

    sends: list[tuple[str, str]] = []
    _patch_tmux(
        monkeypatch,
        supervisor,
        pane_text="I am Polly, ready.",
        sends=sends,
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "list_panes",
        lambda target: [
            _make_pane(window_name="pm-heartbeat", pane_id="%hb"),
        ],
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(heartbeat_launch, "%hb")

    import threading
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=2)

    assert len(sends) == 1, (
        "heartbeat verify-resend into the heartbeat window must still "
        "deliver — this is the legitimate recovery path"
    )
    assert sends[0][0] == "%hb"


def test_persona_verify_resend_per_task_worker_unaffected(
    monkeypatch, tmp_path: Path,
) -> None:
    """#933 — per-task workers (post-#919/#921) bypass persona-verify entirely.

    Worker role has no entry in ``_ROLE_PERSONA_MARKER`` so
    ``_schedule_persona_verify`` short-circuits before spawning the
    background thread. The new #933 guard therefore can't regress the
    per-task worker kickoff path — there is no resend to guard. This
    test pins that contract so future ``_ROLE_PERSONA_MARKER`` edits
    don't accidentally enrol workers and let the guard fire on a
    ``task-<project>-<N>`` pane.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    launch = supervisor.launch_by_session("operator")
    worker_launch = replace(
        launch,
        session=replace(launch.session, role="worker"),
        initial_input="task-pollypm-7 kickoff",
        window_name="task-pollypm-7",
    )

    sends: list[tuple[str, str]] = []
    captures: list[str] = []
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "capture_pane",
        lambda target, lines=50: captures.append(target) or "",
    )
    monkeypatch.setattr(
        supervisor.session_service.tmux,
        "send_keys",
        lambda target, text, **kw: sends.append((target, text)),
    )
    monkeypatch.setattr("pollypm.supervisor.time.sleep", lambda _s: None)

    supervisor._schedule_persona_verify(
        worker_launch, "pollypm-storage-closet:task-pollypm-7",
    )

    import threading
    import time as _time
    _time.sleep(0.05)
    for t in threading.enumerate():
        if t.name.startswith("persona-verify-"):
            t.join(timeout=0.2)

    assert captures == [], (
        "worker role has no persona marker — verify thread must not spawn"
    )
    assert sends == []
