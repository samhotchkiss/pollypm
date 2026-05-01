"""Regression tests for issue #1005 — auto-recovery for ``<role>/no_session``.

Two behaviours covered:

1. The bootstrap text the supervisor + tmux session-service write into
   their kickoff pane no longer carries the
   "[PollyPM bootstrap — system message, please ignore on screen]"
   framing header. The header tripped Claude's prompt-injection defense
   and the model refused to adopt its own bootstrap as instructions.
2. ``auto_recover_no_session_alerts`` walks open ``no_session`` alerts,
   honours the alert-age threshold + per-(role, project) backoff, calls
   the spawn shim, records attempt events, and escalates to
   ``<role>/no_session_spawn_failed`` once the attempt budget exhausts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pollypm.recovery.no_session_spawn import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_THRESHOLD_SECONDS,
    SPAWN_ATTEMPT_EVENT_TYPE,
    SPAWN_FAILED_ALERT_TYPE,
    auto_recover_no_session_alerts,
)


# ---------------------------------------------------------------------------
# Bug A — bootstrap framing
# ---------------------------------------------------------------------------


_REJECTED_FRAMING_LITERAL = (
    "\"[PollyPM bootstrap — system message, please ignore on screen]\""
)
# #1007: the v2 reformat ("Adopt both files as your operating
# instructions and reply only \"ready\" when done.") was *also*
# flagged by Claude's injection defense, so it can't appear in the
# active bootstrap path either. Keep the v1 + v2 tells separate so a
# regression to either one is identified by the right history note.
_REJECTED_V2_OPERATING_INSTRUCTIONS = (
    "Adopt both files as your operating instructions and reply only"
)
_REJECTED_V2_OPERATING_INSTRUCTIONS_SINGULAR = (
    "adopt it as your operating instructions, then reply only"
)


def _active_format_strings(source: str) -> str:
    """Strip Python comments + docstrings so #1005/#1007 history notes
    that *name* the rejected framing for archaeology purposes don't
    trip the regression assertions on those literals.

    Cheap heuristic: drop every line whose first non-whitespace char
    is ``#``, then walk triple-quoted blocks and drop those too. The
    bootstrap format strings live inside ``return (\"…\")``
    expressions so they survive both filters.
    """
    out_lines: list[str] = []
    in_triple = False
    triple_marker = ""
    for line in source.splitlines():
        if not in_triple:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Detect start of a triple-quoted block on this line.
            for marker in ('"""', "'''"):
                if marker in line:
                    # Inline """…""" on a single line — drop just that line.
                    after_open = line.split(marker, 1)[1]
                    if marker in after_open:
                        # Single-line docstring; drop entire line.
                        continue
                    in_triple = True
                    triple_marker = marker
                    break
            if in_triple:
                continue
            out_lines.append(line)
        else:
            if triple_marker in line:
                in_triple = False
                triple_marker = ""
                continue
    return "\n".join(out_lines)


def test_supervisor_bootstrap_drops_rejected_framing_history() -> None:
    """The supervisor's *active* bootstrap text must not carry either
    rejected framing:

    * v1 "[PollyPM bootstrap — system message, please ignore on
      screen]" (#1005 — fake-system-authority header).
    * v2 "Adopt both files as your operating instructions and reply
      only 'ready' when done." (#1007 — same category, just less
      decorated; modern Claude rejects the operating-instructions +
      reply-only-'ready' pattern regardless of header).

    The path substring ``/control-prompts/<session>.md`` relied on by
    :func:`transcript_matches_session` (#935) is preserved.
    """
    raw = (
        Path(__file__).resolve().parents[1]
        / "src" / "pollypm" / "supervisor.py"
    ).read_text(encoding="utf-8")
    active = _active_format_strings(raw)
    assert _REJECTED_FRAMING_LITERAL not in active, (
        "v1 [PollyPM bootstrap — system message] header is back in an "
        "active bootstrap string — see #1005."
    )
    assert _REJECTED_V2_OPERATING_INSTRUCTIONS not in active, (
        "v2 'Adopt both files as your operating instructions and reply "
        "only \"ready\"' phrasing is back — see #1007."
    )
    assert _REJECTED_V2_OPERATING_INSTRUCTIONS_SINGULAR not in active, (
        "v2 single-file 'adopt it as your operating instructions, then "
        "reply only \"ready\"' phrasing is back — see #1007."
    )
    # Path substring used by the resume-attribution helper (#935) must
    # still appear so a transcript can be attributed to its session.
    assert "control-prompts" in raw


def test_tmux_session_service_bootstrap_drops_rejected_framing_history() -> None:
    raw = (
        Path(__file__).resolve().parents[1]
        / "src" / "pollypm" / "session_services" / "tmux.py"
    ).read_text(encoding="utf-8")
    active = _active_format_strings(raw)
    assert _REJECTED_FRAMING_LITERAL not in active
    assert _REJECTED_V2_OPERATING_INSTRUCTIONS not in active
    assert _REJECTED_V2_OPERATING_INSTRUCTIONS_SINGULAR not in active
    assert "control-prompts" in raw


# ---------------------------------------------------------------------------
# Bug B — no_session auto-recovery
# ---------------------------------------------------------------------------


@dataclass
class _FakeAlert:
    session_name: str
    alert_type: str
    severity: str
    message: str
    status: str
    created_at: str
    updated_at: str
    alert_id: int | None = None


@dataclass
class _FakeEvent:
    session_name: str
    event_type: str
    message: str
    created_at: str


@dataclass
class _FakeStore:
    """Minimal store double — only the methods auto-recovery touches."""

    alerts: list[_FakeAlert] = field(default_factory=list)
    events: list[_FakeEvent] = field(default_factory=list)
    upserted: list[tuple[str, str, str, str]] = field(default_factory=list)
    cleared: list[tuple[str, str]] = field(default_factory=list)

    def open_alerts(self) -> list[_FakeAlert]:
        return [a for a in self.alerts if a.status == "open"]

    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.upserted.append((session_name, alert_type, severity, message))
        for alert in self.alerts:
            if (
                alert.session_name == session_name
                and alert.alert_type == alert_type
                and alert.status == "open"
            ):
                alert.message = message
                alert.severity = severity
                return
        self.alerts.append(
            _FakeAlert(
                session_name=session_name,
                alert_type=alert_type,
                severity=severity,
                message=message,
                status="open",
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.cleared.append((session_name, alert_type))
        for alert in self.alerts:
            if (
                alert.session_name == session_name
                and alert.alert_type == alert_type
                and alert.status == "open"
            ):
                alert.status = "closed"

    def record_event(
        self, session_name: str, event_type: str, message: str,
    ) -> None:
        self.events.append(
            _FakeEvent(
                session_name=session_name,
                event_type=event_type,
                message=message,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def recent_events(self, limit: int = 20) -> list[_FakeEvent]:
        return list(reversed(self.events))[:limit]


@dataclass
class _FakeProject:
    key: str


@dataclass
class _FakeServices:
    msg_store: _FakeStore
    state_store: _FakeStore | None = None
    known_projects: tuple[Any, ...] = ()
    config: Any = None


def _make_no_session_alert(
    *,
    session_name: str = "reviewer",
    role: str = "reviewer",
    project: str = "bikepath",
    age_seconds: int = 120,
) -> _FakeAlert:
    now = datetime.now(timezone.utc)
    created = (now - timedelta(seconds=age_seconds)).isoformat()
    return _FakeAlert(
        session_name=session_name,
        alert_type="no_session",
        severity="warn",
        message=(
            f"No worker is running for the {role} role on '{project}' — "
            f"task {project}/8 is stuck in the queue. "
            "Open Tasks or Inbox and use Approve or Reject."
        ),
        status="open",
        created_at=created,
        updated_at=created,
    )


def test_auto_recovery_skips_alerts_younger_than_threshold() -> None:
    """A fresh ``no_session`` alert (age < 60s) is left alone — gives
    the task-claim path a moment to win the race before we step in."""
    store = _FakeStore(
        alerts=[
            _make_no_session_alert(age_seconds=DEFAULT_THRESHOLD_SECONDS - 5)
        ],
    )
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/dev/null"), spawn=fake_spawn,
    )
    assert spawn_calls == []
    assert [d.outcome for d in decisions] == ["skipped_young"]


def test_auto_recovery_skips_worker_role() -> None:
    """worker-role ``no_session`` alerts are NOT auto-spawned — per-task
    workers come from ``pm task claim``, not ``pm worker-start``."""
    alert = _make_no_session_alert(role="worker", session_name="worker-bikepath")
    store = _FakeStore(alerts=[alert])
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/dev/null"), spawn=fake_spawn,
    )
    assert spawn_calls == []
    assert [d.outcome for d in decisions] == ["skipped_role"]


def test_auto_recovery_skips_unknown_projects() -> None:
    """Ghost projects (alert references a project not in the registry) are
    left to the existing ``_sweep_ghost_project_alerts`` cleanup."""
    store = _FakeStore(alerts=[_make_no_session_alert(project="ghostproj")])
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/dev/null"), spawn=fake_spawn,
    )
    assert spawn_calls == []
    assert [d.outcome for d in decisions] == ["skipped_unknown_project"]


def test_auto_recovery_spawns_reviewer_after_threshold() -> None:
    """The canonical case: a reviewer ``no_session`` alert open for >60s
    triggers a spawn attempt."""
    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )
    spawn_calls: list[tuple[str, str, Path]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project, config_path))
        return True, "session=reviewer_bikepath"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    assert spawn_calls == [("reviewer", "bikepath", Path("/tmp/cfg.toml"))]
    assert [d.outcome for d in decisions] == ["spawned"]
    assert decisions[0].attempt_number == 1
    # Attempt event recorded so the next tick sees prior history.
    assert any(
        ev.event_type == SPAWN_ATTEMPT_EVENT_TYPE for ev in store.events
    )


def test_auto_recovery_records_failed_attempt_and_backs_off() -> None:
    """A failed spawn records an attempt event; the next tick (within
    backoff window) skips re-attempting."""
    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )

    def fake_spawn_fail(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        return False, "create_worker_session failed: account_unavailable"

    # First tick: attempts and fails.
    decisions1 = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn_fail,
    )
    assert [d.outcome for d in decisions1] == ["spawn_failed"]
    assert decisions1[0].attempt_number == 1

    # Second tick fired immediately — backoff still active.
    decisions2 = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn_fail,
    )
    assert [d.outcome for d in decisions2] == ["skipped_backoff"]


def test_auto_recovery_escalates_after_max_attempts() -> None:
    """After ``DEFAULT_MAX_ATTEMPTS`` failed spawns, escalate to the
    ``no_session_spawn_failed`` alert family and stop trying."""
    # Seed an aged alert + N attempt events recorded AFTER the alert
    # opened (so the per-episode counter actually counts them — see the
    # ``since`` parameter on ``_attempt_history``).
    alert = _make_no_session_alert(age_seconds=600)
    store = _FakeStore(alerts=[alert])
    alert_opened = datetime.fromisoformat(alert.created_at)
    for n in range(DEFAULT_MAX_ATTEMPTS):
        store.events.append(
            _FakeEvent(
                session_name="reviewer",
                event_type=SPAWN_ATTEMPT_EVENT_TYPE,
                message=f"prior attempt {n}",
                created_at=(
                    alert_opened + timedelta(seconds=10 + n)
                ).isoformat(),
            )
        )
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )

    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    assert spawn_calls == []  # didn't try
    assert [d.outcome for d in decisions] == ["escalated"]
    # Sibling alert raised so a human picks up.
    assert any(
        upsert[1] == SPAWN_FAILED_ALERT_TYPE
        for upsert in store.upserted
    )


def test_auto_recovery_clears_spawn_failed_after_success() -> None:
    """When auto-spawn finally succeeds, the ``no_session_spawn_failed``
    sibling alert is cleared so the cockpit reflects the recovery."""
    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )

    def fake_spawn_ok(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        return True, "session=reviewer_bikepath"

    auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn_ok,
    )
    assert (
        ("reviewer", SPAWN_FAILED_ALERT_TYPE) in store.cleared
    )


def test_auto_recovery_resets_attempt_counter_per_alert_episode() -> None:
    """An attempt event recorded BEFORE the alert's ``created_at`` is
    treated as belonging to a previous recovery episode and does NOT
    count against the current attempt budget. This means a transient
    re-open after a successful recovery starts fresh, not pre-exhausted.
    """
    alert = _make_no_session_alert(age_seconds=600)
    store = _FakeStore(alerts=[alert])
    alert_opened = datetime.fromisoformat(alert.created_at)
    # Pre-seed N events from a *prior* episode (older than the current
    # alert's created_at).
    for n in range(DEFAULT_MAX_ATTEMPTS + 2):
        store.events.append(
            _FakeEvent(
                session_name="reviewer",
                event_type=SPAWN_ATTEMPT_EVENT_TYPE,
                message=f"prior episode attempt {n}",
                created_at=(
                    alert_opened - timedelta(hours=1, seconds=n)
                ).isoformat(),
            )
        )
    services = _FakeServices(
        msg_store=store, known_projects=(_FakeProject("bikepath"),),
    )

    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str) -> tuple[bool, str]:
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    # Counter reset to 0 → first attempt fires, not escalation.
    assert spawn_calls == [("reviewer", "bikepath")]
    assert [d.outcome for d in decisions] == ["spawned"]
    assert decisions[0].attempt_number == 1


def test_auto_recovery_no_op_when_store_missing() -> None:
    services = _FakeServices(msg_store=None, state_store=None)  # type: ignore[arg-type]
    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"),
    )
    assert decisions == []


# ---------------------------------------------------------------------------
# #1011 — resolver matches the per-project session the spawn creates
# ---------------------------------------------------------------------------


def test_role_candidate_names_includes_per_project_for_reviewer() -> None:
    """Regression for #1011 Defect A.

    ``pm worker-start --role reviewer <project>`` (the same dispatch
    used by #1005's auto-recovery) creates a per-project
    ``reviewer_<project>`` session. Pre-fix ``role_candidate_names``
    returned only the workspace-singleton ``["reviewer", "pm-reviewer"]``
    list, so the resolver never matched the spawned session and the
    ``no_session`` alert kept re-firing — leading to orphan
    ``reviewer-<project>`` windows and an exhausted recovery budget.

    The post-fix list MUST include ``reviewer_<project>`` (and
    ``reviewer-<project>`` for naming-drift tolerance) BEFORE the
    singleton candidates, so the next sweep tick after a successful
    spawn finds the new session and the alert clears.
    """
    from pollypm.work.task_assignment import role_candidate_names

    candidates = role_candidate_names("reviewer", "bikepath")
    # Per-project candidates first so the spawned session resolves
    # before the legacy singleton names.
    assert candidates[0] == "reviewer_bikepath"
    assert "reviewer-bikepath" in candidates
    # Singleton fallback retained — workspaces with a singleton
    # ``reviewer`` session still resolve.
    assert "reviewer" in candidates
    assert "pm-reviewer" in candidates


def test_role_candidate_names_per_project_resolves_spawned_session() -> None:
    """End-to-end resolver check: a live ``reviewer_<project>`` session
    (the shape ``pm worker-start --role reviewer <project>`` creates)
    is found by ``SessionRoleIndex.resolve`` for the canonical
    ``role:reviewer`` lookup when a project key is supplied. Pre-#1011
    the resolver returned ``None`` because the candidate list was
    singleton-only.
    """
    from pollypm.work.models import ActorType
    from pollypm.work.task_assignment import SessionRoleIndex

    @dataclass
    class _Handle:
        name: str

    class _SessionService:
        def list(self) -> list[_Handle]:
            return [_Handle(name="reviewer_bikepath")]

    index = SessionRoleIndex(_SessionService())
    handle = index.resolve(ActorType.ROLE, "reviewer", "bikepath")
    assert handle is not None
    assert handle.name == "reviewer_bikepath"
