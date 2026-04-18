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

    rows = supervisor.store.execute(
        "SELECT event_type, message FROM events "
        "WHERE session_name = ? AND event_type = ?",
        ("operator", "persona_swap_detected"),
    ).fetchall()
    assert len(rows) == 1
    _event_type, message = rows[0]
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

    events = supervisor.store.execute(
        "SELECT event_type FROM events WHERE session_name = 'operator'",
    ).fetchall()
    assert not any(row[0] == "persona_swap_verified" for row in events)


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
    events = supervisor.store.execute(
        "SELECT event_type, message FROM events "
        "WHERE session_name = 'operator' AND event_type = 'persona_swap_verified'",
    ).fetchall()
    assert len(events) == 1
    assert "Russell" in events[0][1]


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
