"""Issue #2068 — sessions-admin pause marker wired into daemon loops.

Covers the partial wiring now present:

1. ``pollypm.session_paused`` reader contract (``is_paused`` /
   ``load_paused_names`` / ``skip_if_paused``).
2. ``pollypm.recovery.no_session_spawn.auto_recover_no_session_alerts``
   honours the marker — no spawn fires for a paused expected-session.
3. ``pollypm.supervisor.Supervisor.maybe_recover_session`` honours the
   marker — no policy-recommendation / restart side effects when the
   session is paused.
4. Direct supervisor relaunch/window creation and heartbeat
   per-session processing honour the same shared helper.

Remaining direct/manual cockpit and chat sends are explicit operator
actions unless routed through those daemon-loop chokepoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixture: a config-shaped object with a writable base_dir
# ---------------------------------------------------------------------------


@dataclass
class _FakeProject:
    """Test double for ``ProjectSettings`` — shape-compatible with the real
    config slice the pause-marker module reads.

    PR #2081 round 3: ``_emit_marker_diagnostic`` now routes through
    :func:`pollypm.audit.log.emit`, which keys the per-project log off
    ``root_dir`` (``<root_dir>/.pollypm/audit.jsonl``) and the central-
    tail mirror off the project ``name``. So we carry both:

    * ``root_dir`` — the project root the audit facade hangs the
      per-project log off.
    * ``base_dir`` — ``<root_dir>/.pollypm`` (real-world convention),
      where ``paused-sessions.json`` lives.
    * ``name`` — project key used as the central-tail filename.
    """

    base_dir: Path
    root_dir: Path
    name: str = "demo"
    # Legacy attr the older test fixtures keyed on. Real ProjectSettings
    # doesn't carry ``key``; retain for any callers reading it as
    # belt-and-suspenders.
    key: str = "demo"


@dataclass
class _FakeConfig:
    project: _FakeProject


@pytest.fixture
def config_with_base_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> _FakeConfig:
    # Mirror production layout: ``base_dir == <root_dir>/.pollypm`` so
    # the canonical audit writer (which keys the per-project log off
    # ``<root_dir>/.pollypm/audit.jsonl``) lands the marker diagnostics
    # next to the marker itself.
    root = tmp_path / "proj"
    root.mkdir()
    base = root / ".pollypm"
    base.mkdir()
    # Redirect the central-tail mirror so the test never touches the
    # user's real ``~/.pollypm/audit/`` tree.
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit-home"))
    return _FakeConfig(
        project=_FakeProject(base_dir=base, root_dir=root),
    )


@pytest.fixture(autouse=True)
def _reset_pause_throttle_between_tests() -> Any:
    """Throttle / marker-state-transition bookkeeping is process-global
    (it has to be, to dedupe across module-level callers). Reset it
    between each test so ordering can't leak emit/no-emit assertions
    from one case into the next."""
    from pollypm.session_paused import _reset_skip_throttle_for_tests

    _reset_skip_throttle_for_tests()
    yield
    _reset_skip_throttle_for_tests()


def _write_marker(config: _FakeConfig, names: list[str]) -> Path:
    """Write the pause marker the way ``sessions_admin._write_paused_names``
    would — a JSON list of names sitting at ``<base_dir>/paused-sessions.json``."""
    path = config.project.base_dir / "paused-sessions.json"
    path.write_text(json.dumps(sorted(names), indent=2) + "\n")
    return path


def _read_pause_skip_audit_events(config: _FakeConfig) -> list[Any]:
    from pollypm.audit.log import read_events
    from pollypm.session_paused import PAUSE_SKIP_EVENT_TYPE

    return read_events(
        config.project.name,
        project_path=config.project.root_dir,
        event=PAUSE_SKIP_EVENT_TYPE,
        limit=20,
    )


# ---------------------------------------------------------------------------
# Unit — session_paused reader
# ---------------------------------------------------------------------------


def test_load_paused_names_returns_empty_when_marker_missing(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import load_paused_names

    # No file written yet → empty set, not an exception.
    assert load_paused_names(config_with_base_dir) == set()


def test_load_paused_names_reads_marker(config_with_base_dir: _FakeConfig) -> None:
    from pollypm.session_paused import load_paused_names

    _write_marker(config_with_base_dir, ["operator", "reviewer-demo"])
    assert load_paused_names(config_with_base_dir) == {
        "operator", "reviewer-demo",
    }


def test_load_paused_names_handles_malformed_json(
    config_with_base_dir: _FakeConfig,
) -> None:
    """``load_paused_names`` keeps its best-effort empty-set degrade for
    legacy set-shaped callers. Recovery/API callers go through
    :func:`is_paused` / :func:`load_paused_state` instead, which fail
    CLOSED (covered below)."""
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, load_paused_names,
    )

    _reset_skip_throttle_for_tests()
    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        "{not json}"
    )
    # The legacy set-shaped load remains empty; fail-closed callers use
    # ``load_paused_state`` / ``is_paused`` instead.
    assert load_paused_names(config_with_base_dir) == set()


def test_load_paused_names_handles_no_base_dir() -> None:
    from pollypm.session_paused import load_paused_names

    class _NullProject:
        base_dir = None

    class _NullConfig:
        project = _NullProject()

    assert load_paused_names(_NullConfig()) == set()


# ---------------------------------------------------------------------------
# Unit — load_paused_state (discriminated marker state, PR #2081 r2)
# ---------------------------------------------------------------------------


def test_load_paused_state_absent_when_no_file(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, load_paused_state,
    )

    _reset_skip_throttle_for_tests()
    state = load_paused_state(config_with_base_dir)
    assert state.kind == "absent"
    assert state.names == frozenset()


def test_load_paused_state_ok_when_file_parses(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, load_paused_state,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator", "reviewer-demo"])
    state = load_paused_state(config_with_base_dir)
    assert state.kind == "ok"
    assert state.names == frozenset({"operator", "reviewer-demo"})


def test_load_paused_state_unreadable_on_malformed_json(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, load_paused_state,
    )

    _reset_skip_throttle_for_tests()
    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        "{not json}"
    )
    state = load_paused_state(config_with_base_dir)
    assert state.kind == "unreadable"
    assert "malformed JSON" in state.reason


def test_load_paused_state_unreadable_on_wrong_shape(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A JSON document that parses but is not a list collapses to
    ``unreadable`` — we don't want to silently accept a mis-shaped
    marker as 'no sessions paused'."""
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, load_paused_state,
    )

    _reset_skip_throttle_for_tests()
    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        '{"paused": ["operator"]}'
    )
    state = load_paused_state(config_with_base_dir)
    assert state.kind == "unreadable"


# ---------------------------------------------------------------------------
# Unit — is_paused FAIL-CLOSED on unreadable marker (PR #2081 r2)
# ---------------------------------------------------------------------------


def test_is_paused_true_when_name_listed(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "operator") is True


def test_is_paused_false_when_name_not_listed(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "reviewer") is False


def test_is_paused_false_when_marker_missing(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    assert is_paused(config_with_base_dir, "operator") is False


def test_is_paused_fails_closed_on_malformed_marker(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A corrupt pause marker must NOT let recovery restart a session
    the operator intended to keep paused.

    Previously the helper degraded to 'no sessions paused' on bad JSON
    (Codex PR #2081 round 1) — Codex round 2 flagged this as a safety
    bug now that the marker gates the supervisor / no-session-spawn
    apply paths. The fix: treat unreadable as 'everything paused' so
    the recovery loops yield until the operator repairs the marker.
    """
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        "{not json}"
    )
    # Any session name returns True — fail closed.
    assert is_paused(config_with_base_dir, "operator") is True
    assert is_paused(config_with_base_dir, "reviewer") is True
    assert is_paused(
        config_with_base_dir, "never-configured-session",
    ) is True


def test_is_paused_fails_closed_on_permission_error(
    config_with_base_dir: _FakeConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission-denied read on the marker also fails closed.

    We can't reliably chmod a file to 000 on every CI surface (root,
    container variants), so we monkeypatch ``Path.read_text`` to
    raise the same ``OSError`` the read would surface in production.
    """
    from pathlib import Path as _Path

    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    orig_read_text = _Path.read_text

    def boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if self.name == "paused-sessions.json":
            raise PermissionError("simulated denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "read_text", boom)

    # Even a name we KNOW isn't in the original list returns True —
    # the recovery loop has no way to verify, so it yields.
    assert is_paused(config_with_base_dir, "operator") is True
    assert is_paused(
        config_with_base_dir, "definitely-not-in-marker",
    ) is True


@pytest.mark.skipif(
    __import__("os").geteuid() == 0,
    reason="root bypasses chmod 0o000 so the permission denial never fires",
)
def test_is_paused_fails_closed_on_real_chmod_permission_denied(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Real ``chmod 0o000`` regression — Codex PR #2081 round 4.

    The monkeypatched ``read_text`` test above proves the OSError branch
    wires through ``MarkerState.unreadable``, but it CANNOT catch the
    Python 3.14 regression where ``Path.exists()`` itself raises
    ``PermissionError`` on a permission-denied ``stat()`` (the pre-fix
    code path ran ``path.exists()`` BEFORE the try block, so a 3.14
    stat-raises permission error escaped the discriminated read).

    This test exercises the real OS-level denial: chmod the marker to
    ``0o000`` and verify the helper still:

    * returns ``MarkerState.unreadable`` (fail-closed contract),
    * makes ``is_paused`` return True for ANY session, and
    * emits the canonical ``session.pause.marker_unreadable`` audit
      diagnostic.
    """
    import os

    from pollypm.audit.log import SCHEMA_VERSION
    from pollypm.session_paused import (
        PAUSE_MARKER_UNREADABLE_EVENT_TYPE,
        _reset_skip_throttle_for_tests,
        is_paused,
        load_paused_state,
    )

    _reset_skip_throttle_for_tests()
    marker = _write_marker(config_with_base_dir, ["operator"])
    original_mode = marker.stat().st_mode & 0o777
    try:
        os.chmod(marker, 0o000)
        # Sanity: confirm the OS actually denies us. If the platform
        # silently grants read regardless (some FUSE / CI surfaces do),
        # skip rather than emit a false negative.
        try:
            with open(marker, "rb"):
                pytest.skip(
                    "platform/filesystem ignored chmod 0o000 — cannot "
                    "exercise real permission-denied path",
                )
        except PermissionError:
            pass

        state = load_paused_state(config_with_base_dir)
        assert state.kind == "unreadable", state
        # is_paused fails closed for ANY session name, including ones
        # we know are not in the marker list.
        assert is_paused(config_with_base_dir, "operator") is True
        assert is_paused(
            config_with_base_dir, "definitely-not-in-marker",
        ) is True

        # Canonical audit diagnostic landed on the project's audit.jsonl.
        audit_path = (
            config_with_base_dir.project.base_dir / "audit.jsonl"
        )
        assert audit_path.exists(), "expected audit.jsonl to be emitted"
        rows = [
            json.loads(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        unreadable_rows = [
            r for r in rows
            if r.get("event") == PAUSE_MARKER_UNREADABLE_EVENT_TYPE
        ]
        assert len(unreadable_rows) >= 1, rows
        row = unreadable_rows[0]
        assert row["schema"] == SCHEMA_VERSION
        assert row["status"] == "warn"
        assert row["actor"] == "system"
        # The reason carries the OSError class name so an operator can
        # tell a permission denial apart from a JSON parse failure.
        reason = row["metadata"].get("reason", "")
        assert "PermissionError" in reason or "Errno 13" in reason, reason
    finally:
        # Restore so tmp_path teardown can clean up.
        try:
            os.chmod(marker, original_mode or 0o600)
        except OSError:
            pass


def test_is_paused_unreadable_emits_audit_event_once(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The ``session.pause.marker_unreadable`` audit event lands on the
    project's ``audit.jsonl`` so an operator can see WHY the loops are
    failing closed — even when the recovery loop never passes a
    ``store`` handle into ``is_paused``. Repeated reads within the
    throttle window must not duplicate the row (Codex PR #2081 r2
    finding 1).

    Codex PR #2081 round 3 — finding 1: the diagnostic must use the
    canonical audit schema (``schema``, ISO ``ts``, ``event``,
    ``subject``, ``actor``, ``status``, ``metadata``) so anything
    grepping audit events by ``event`` sees the diagnostic. The
    previous ad-hoc ``{ts: float, event_type, subject, payload}``
    writer is gone.
    """
    from pollypm.audit.log import SCHEMA_VERSION
    from pollypm.session_paused import (
        PAUSE_MARKER_UNREADABLE_EVENT_TYPE,
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        "{not json}"
    )
    # Hammer the read 20 times.
    for _ in range(20):
        assert is_paused(config_with_base_dir, "operator") is True

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    assert audit_path.exists(), "expected audit.jsonl to be emitted"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines() if line.strip()
    ]
    unreadable_rows = [
        r for r in rows
        if r.get("event") == PAUSE_MARKER_UNREADABLE_EVENT_TYPE
    ]
    # Exactly one despite 20 reads — throttle held.
    assert len(unreadable_rows) == 1, rows
    row = unreadable_rows[0]
    # Canonical schema: schema int, ISO ts, project key, event/subject/
    # actor/status/metadata keys.
    assert row["schema"] == SCHEMA_VERSION
    assert isinstance(row["ts"], str) and "T" in row["ts"], row["ts"]
    assert row["project"] == "demo"
    assert row["subject"]  # non-empty, carries the marker path + reason
    assert row["actor"] == "system"
    assert row["status"] == "warn"
    assert isinstance(row["metadata"], dict)
    assert "reason" in row["metadata"]
    assert "path" in row["metadata"]
    # The ad-hoc keys must NOT appear — these were the bypass shape.
    assert "event_type" not in row
    assert "payload" not in row


def test_is_paused_unreadable_throttle_survives_process_restart(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A one-shot heartbeat process must not reset the 5 min throttle."""
    from pollypm.session_paused import (
        PAUSE_MARKER_UNREADABLE_EVENT_TYPE,
        _reset_skip_throttle_for_tests,
        is_paused,
    )

    marker = config_with_base_dir.project.base_dir / "paused-sessions.json"
    marker.write_text("{not json}")
    assert is_paused(config_with_base_dir, "operator") is True

    # Simulate the next heartbeat invocation in a fresh process: the
    # in-memory transition/throttle dictionaries are gone, but the
    # canonical audit log remains.
    _reset_skip_throttle_for_tests()
    assert is_paused(config_with_base_dir, "operator") is True

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
    ]
    unreadable_rows = [
        row
        for row in rows
        if row.get("event") == PAUSE_MARKER_UNREADABLE_EVENT_TYPE
    ]
    assert len(unreadable_rows) == 1, rows


def test_is_paused_unreadable_to_readable_emits_restored(
    config_with_base_dir: _FakeConfig,
) -> None:
    """When the operator repairs a corrupt marker the helper must
    emit ``session.pause.marker_restored`` so the audit trail shows
    the recovery loops returned to their normal gating.

    Both diagnostics use the canonical audit schema (PR #2081 round
    3 — finding 1).
    """
    from pollypm.audit.log import SCHEMA_VERSION
    from pollypm.session_paused import (
        PAUSE_MARKER_RESTORED_EVENT_TYPE,
        PAUSE_MARKER_UNREADABLE_EVENT_TYPE,
        _reset_skip_throttle_for_tests, is_paused,
    )

    _reset_skip_throttle_for_tests()
    marker = (
        config_with_base_dir.project.base_dir / "paused-sessions.json"
    )
    marker.write_text("{not json}")
    assert is_paused(config_with_base_dir, "operator") is True

    # Repair it.
    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "operator") is True  # still paused
    assert is_paused(config_with_base_dir, "reviewer") is False  # no fail-closed

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines() if line.strip()
    ]
    events = [r.get("event") for r in rows]
    assert PAUSE_MARKER_UNREADABLE_EVENT_TYPE in events
    assert PAUSE_MARKER_RESTORED_EVENT_TYPE in events
    restored = next(
        r for r in rows
        if r.get("event") == PAUSE_MARKER_RESTORED_EVENT_TYPE
    )
    # Restored transitions ride the same canonical schema.
    assert restored["schema"] == SCHEMA_VERSION
    assert restored["status"] == "ok"
    assert restored["actor"] == "system"
    assert restored["metadata"]["kind"] in {"ok", "absent"}


def test_marker_restored_survives_process_restart(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Restored detection must use the durable audit log, not memory."""
    from pollypm.session_paused import (
        PAUSE_MARKER_RESTORED_EVENT_TYPE,
        _reset_skip_throttle_for_tests,
        is_paused,
    )

    marker = config_with_base_dir.project.base_dir / "paused-sessions.json"
    marker.write_text("{not json}")
    assert is_paused(config_with_base_dir, "operator") is True

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "operator") is True

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
    ]
    restored_rows = [
        row
        for row in rows
        if row.get("event") == PAUSE_MARKER_RESTORED_EVENT_TYPE
    ]
    assert len(restored_rows) == 1, rows
    metadata = restored_rows[0]["metadata"]
    assert metadata["restored_size_bytes"] > 0
    assert metadata["restored_at"]
    assert metadata["restored_mtime"]
    assert metadata["paused_names"] == ["operator"]
    assert metadata["paused_count"] == 1
    assert metadata["paused_state_diff"]["current_state"] == "ok"


def test_marker_restored_metadata_includes_session_diff(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        PAUSE_MARKER_RESTORED_EVENT_TYPE,
        is_paused,
    )

    marker = _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "operator") is True
    marker.write_text("{not json}")
    assert is_paused(config_with_base_dir, "operator") is True
    _write_marker(config_with_base_dir, ["reviewer"])
    assert is_paused(config_with_base_dir, "reviewer") is True

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
    ]
    restored = next(
        row
        for row in rows
        if row.get("event") == PAUSE_MARKER_RESTORED_EVENT_TYPE
    )
    diff = restored["metadata"]["paused_state_diff"]
    assert diff["previous_names_known"] is True
    assert diff["added_paused"] == ["reviewer"]
    assert diff["removed_paused"] == ["operator"]


def test_skip_if_paused_returns_false_for_unpaused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    events: list[tuple] = []

    class _Store:
        def record_event(self, *args, **kwargs):  # noqa: ANN002,ANN003
            events.append((args, kwargs))

    # No marker → no skip, no audit emission.
    assert skip_if_paused(
        config_with_base_dir, "operator", store=_Store(),
        loop="unit_test",
    ) is False
    assert events == []


def test_skip_if_paused_emits_audit_event_on_skip(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The audit event lets the cockpit show that the loop honoured the
    pause — without it, a skipped recovery is invisible."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE,
        _reset_skip_throttle_for_tests,
        skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    assert skip_if_paused(
        config_with_base_dir, "operator", store=_Store(),
        loop="unit_test", reason="failure_type=missing_window",
    ) is True
    assert len(captured) == 1
    event = captured[0]
    assert event["sender"] == PAUSE_SKIP_EVENT_TYPE
    assert event["scope"] == "operator"
    assert "operator" in event["subject"]
    assert "unit_test" in event["subject"]
    assert event["payload"]["loop"] == "unit_test"
    assert event["payload"]["reason"] == "failure_type=missing_window"
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(audit_events) == 1
    audit_event = audit_events[0]
    assert audit_event.event == PAUSE_SKIP_EVENT_TYPE
    assert audit_event.project == "demo"
    assert audit_event.subject == event["subject"]
    assert audit_event.actor == "system"
    assert audit_event.status == "ok"
    assert audit_event.metadata == event["payload"]


def test_skip_if_paused_no_store_still_returns_true(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Audit emission is optional — pure-boolean guard mode must work."""
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])
    # No ``store`` kwarg → no emission, but the guard still trips.
    assert skip_if_paused(
        config_with_base_dir, "operator", loop="boolean_only",
    ) is True


def test_skip_if_paused_throttles_repeat_audit_emission(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A paused session that survives many sweep ticks must NOT pile
    up one ``session.pause.skip`` audit event per tick (Codex PR
    #2081 r2 finding 1). The boolean guard still trips every call —
    only the durable event is throttled per (session, loop)."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE, _reset_skip_throttle_for_tests,
        skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    # Simulate 50 sweep ticks for the same (session, loop) — well past
    # what a real bug would produce in a few minutes of pause.
    for _ in range(50):
        assert skip_if_paused(
            config_with_base_dir, "operator", store=_Store(),
            loop="no_session_spawn.auto_recover",
        ) is True

    skip_events = [
        e for e in captured if e.get("sender") == PAUSE_SKIP_EVENT_TYPE
    ]
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(skip_events) == 1, (
        f"expected 1 throttled skip event, got {len(skip_events)}: "
        f"{skip_events}"
    )
    assert len(audit_events) == 1


def test_skip_if_paused_throttle_survives_process_restart(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Cron-driven ``pm heartbeat`` starts a fresh Python process each
    tick, so the skip throttle must use the durable audit row rather
    than only module globals."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE, _reset_skip_throttle_for_tests,
        skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    store = _Store()
    assert skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="heartbeat.local.process_session",
    ) is True

    # Simulate the next cron tick in a fresh process: in-memory
    # throttle state is gone, but audit.jsonl remains.
    _reset_skip_throttle_for_tests()
    assert skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="heartbeat.local.process_session",
    ) is True

    skip_events = [
        e for e in captured if e.get("sender") == PAUSE_SKIP_EVENT_TYPE
    ]
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(skip_events) == 1, skip_events
    assert len(audit_events) == 1
    assert audit_events[0].metadata["session_name"] == "operator"
    assert (
        audit_events[0].metadata["loop"]
        == "heartbeat.local.process_session"
    )


def test_skip_if_paused_process_restart_emits_after_durable_window(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A fresh process still emits once the durable audit row is older
    than ``PAUSE_SKIP_THROTTLE_SECONDS``."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE,
        PAUSE_SKIP_THROTTLE_SECONDS,
        _reset_skip_throttle_for_tests,
        skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    store = _Store()
    assert skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="heartbeat.local.process_session",
    ) is True

    audit_path = config_with_base_dir.project.base_dir / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
    ]
    old_ts = (
        datetime.now(timezone.utc)
        - timedelta(seconds=PAUSE_SKIP_THROTTLE_SECONDS + 5)
    ).isoformat()
    for row in rows:
        if row.get("event") == PAUSE_SKIP_EVENT_TYPE:
            row["ts"] = old_ts
    audit_path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
        + "\n"
    )

    _reset_skip_throttle_for_tests()
    assert skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="heartbeat.local.process_session",
    ) is True

    skip_events = [
        e for e in captured if e.get("sender") == PAUSE_SKIP_EVENT_TYPE
    ]
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(skip_events) == 2, skip_events
    assert len(audit_events) == 2


def test_skip_if_paused_throttle_is_per_loop(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The throttle key is (session, loop) — so loop 1 and loop 2 each
    get their own first-emit even when they hit the same session in
    the same tick."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE, _reset_skip_throttle_for_tests,
        skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    store = _Store()
    # Two different loops, same session — both should emit once.
    skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="no_session_spawn.auto_recover",
    )
    skip_if_paused(
        config_with_base_dir, "operator", store=store,
        loop="supervisor.maybe_recover_session",
    )
    # Then re-hit each loop a few times — no extra emits.
    for _ in range(5):
        skip_if_paused(
            config_with_base_dir, "operator", store=store,
            loop="no_session_spawn.auto_recover",
        )
        skip_if_paused(
            config_with_base_dir, "operator", store=store,
            loop="supervisor.maybe_recover_session",
        )

    skip_events = [
        e for e in captured if e.get("sender") == PAUSE_SKIP_EVENT_TYPE
    ]
    assert len(skip_events) == 2, skip_events
    loops = {e["payload"]["loop"] for e in skip_events}
    assert loops == {
        "no_session_spawn.auto_recover",
        "supervisor.maybe_recover_session",
    }
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(audit_events) == 2
    assert {event.metadata["loop"] for event in audit_events} == loops


def test_skip_if_paused_swallows_store_errors(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A flaky audit-store write must not break the guard contract.

    The recovery loops use ``skip_if_paused`` as a hard yield gate. If
    the audit emission raises (e.g. a transient DB outage), the boolean
    return MUST still be True — otherwise the loop would re-enter the
    paused session and fire the very intervention we just chose to
    skip.
    """
    from pollypm.session_paused import (
        _reset_skip_throttle_for_tests, skip_if_paused,
    )

    _reset_skip_throttle_for_tests()
    _write_marker(config_with_base_dir, ["operator"])

    class _FlakyStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("simulated transient store outage")

    assert skip_if_paused(
        config_with_base_dir, "operator", store=_FlakyStore(),
        loop="flaky_store",
    ) is True
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(audit_events) == 1
    assert audit_events[0].metadata["loop"] == "flaky_store"


# ---------------------------------------------------------------------------
# Integration — loop 1: auto_recover_no_session_alerts
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
    """Minimal store double covering the contract auto_recover + the
    pause-skip audit emit hit. ``record_event`` accepts BOTH positional
    (legacy) and keyword (unified) shapes so we exercise the same
    fallback the production helpers walk through."""

    alerts: list[_FakeAlert] = field(default_factory=list)
    events: list[_FakeEvent] = field(default_factory=list)
    upserted: list[tuple[str, str, str, str]] = field(default_factory=list)
    cleared: list[tuple[str, str]] = field(default_factory=list)
    kw_events: list[dict[str, Any]] = field(default_factory=list)

    def open_alerts(self) -> list[_FakeAlert]:
        return [a for a in self.alerts if a.status == "open"]

    def record_event(self, *args, **kwargs):  # noqa: ANN002,ANN003
        if kwargs:
            self.kw_events.append(kwargs)
            return
        session_name, event_type, message = args
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

    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.upserted.append((session_name, alert_type, severity, message))

    def clear_alert(self, session_name: str, alert_type: str, **_) -> None:
        self.cleared.append((session_name, alert_type))


@dataclass
class _FakeProjectKey:
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
    project: str = "demo",
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
            f"task {project}/8 is stuck in the queue."
        ),
        status="open",
        created_at=created,
        updated_at=created,
    )


def test_auto_recover_no_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Loop 1 — ``auto_recover_no_session_alerts`` consults the marker.

    The alert keys on a session named ``reviewer``; pausing that name
    via the sessions-admin marker should yield BEFORE the spawn call
    fires. This is the per-issue requirement: the marker is no longer
    informational-only for the auto-spawn loop.
    """
    from pollypm.recovery.no_session_spawn import (
        auto_recover_no_session_alerts,
    )

    _write_marker(config_with_base_dir, ["reviewer"])
    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    # No spawn happened.
    assert spawn_calls == []
    # The outcome label is the new ``skipped_paused``.
    assert [d.outcome for d in decisions] == ["skipped_paused"]
    # The audit event landed on the unified-keyword path.
    assert any(
        ev.get("sender") == "session.pause.skip" for ev in store.kw_events
    ), store.kw_events
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(audit_events) == 1
    assert audit_events[0].metadata["loop"] == "no_session_spawn.auto_recover"


def test_auto_recover_no_session_spawns_when_not_paused(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Control: with no marker, the spawn still fires — guard is opt-in."""
    from pollypm.recovery.no_session_spawn import (
        auto_recover_no_session_alerts,
    )

    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,  # base_dir present but no marker
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    assert spawn_calls == [("reviewer", "demo")]
    assert [d.outcome for d in decisions] == ["spawned"]


def test_auto_recover_no_session_honours_project_scoped_pause_name(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The pause check covers BOTH the alert's session_name AND the
    expected per-project session expansion — pausing either spelling
    should yield."""
    from pollypm.recovery.no_session_spawn import (
        _expected_session_name,
        auto_recover_no_session_alerts,
    )

    # Pause the expected-session expansion, NOT the alert's session_name.
    expected = _expected_session_name("reviewer", "demo")
    assert expected  # sanity — the helper returned something
    _write_marker(config_with_base_dir, [expected])

    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    # If `expected == "reviewer"` (the alert's own session_name) this
    # collapses to the basic case. Either way: no spawn.
    assert spawn_calls == []
    assert [d.outcome for d in decisions] == ["skipped_paused"]


# ---------------------------------------------------------------------------
# Integration — loops 2 & 3: Supervisor.maybe_recover_session
# ---------------------------------------------------------------------------


def test_supervisor_maybe_recover_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Loops 2 & 3 — the supervisor's recovery chokepoint yields.

    ``Supervisor.maybe_recover_session`` is the single apply path the
    periodic health sweep (loop 3) AND the policy intervention path
    (loop 2 — ``DefaultRecoveryPolicy``'s recommendation consumer) both
    route through. Adding the guard here covers both loops without
    threading the marker into the policy class itself (which is sealed
    to stay pure per ``pollypm.recovery.base.RecoveryPolicy``).
    """
    from pollypm.supervisor import Supervisor

    _write_marker(config_with_base_dir, ["operator"])

    # A SessionLaunchSpec-shaped double — we never call into the real
    # one because the guard returns BEFORE any planner / tmux work.
    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "op"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))

    # Build a Supervisor stub that only carries the two attributes the
    # guard reads. We DELIBERATELY skip ``Supervisor.__init__`` (which
    # opens sqlite + plugin host) by using ``__new__`` — the guard runs
    # before any of those collaborators are touched.
    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir

    captured: list[dict[str, Any]] = []

    class _MsgStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

        # Methods the apply path would call AFTER the guard — none
        # should fire. We attach asserting stubs so a regression that
        # drops the guard is loud.
        def append_event(self, **kwargs):  # noqa: ANN003
            raise AssertionError(
                "append_event called after paused-skip guard should "
                f"have yielded: {kwargs}"
            )

        def upsert_alert(self, *args, **kwargs):  # noqa: ANN002,ANN003
            raise AssertionError(
                "upsert_alert called after paused-skip guard should "
                f"have yielded: {args} {kwargs}"
            )

        def clear_alert(self, *args, **kwargs):  # noqa: ANN002,ANN003
            raise AssertionError("clear_alert called after pause guard")

    sup._msg_store = _MsgStore()

    # Invoke. Should return cleanly with NO side effects beyond the
    # audit event.
    sup.maybe_recover_session(
        launch, failure_type="missing_window",
        failure_message="window missing",
    )

    assert len(captured) == 1, captured
    event = captured[0]
    assert event["sender"] == "session.pause.skip"
    assert event["scope"] == "operator"
    assert "supervisor.maybe_recover_session" in event["subject"]
    assert event["payload"]["loop"] == "supervisor.maybe_recover_session"
    assert "missing_window" in event["payload"]["reason"]
    audit_events = _read_pause_skip_audit_events(config_with_base_dir)
    assert len(audit_events) == 1
    assert (
        audit_events[0].metadata["loop"]
        == "supervisor.maybe_recover_session"
    )


def test_supervisor_maybe_recover_session_proceeds_when_not_paused(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Control: without a marker, the guard yields False and the
    existing apply path runs. We assert by observing that the policy
    recommendation lookup is reached (it raises in our stub setup;
    proving the guard didn't short-circuit).
    """
    from pollypm.supervisor import Supervisor

    # NO marker written → guard returns False.

    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "op"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))

    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir

    reached_policy: list[str] = []

    class _MsgStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            # Should NOT see the pause.skip event when not paused.
            assert kwargs.get("sender") != "session.pause.skip", kwargs

        def append_event(self, **kwargs):  # noqa: ANN003
            reached_policy.append(kwargs.get("subject", ""))
            # Raise after recording so the rest of the apply path
            # doesn't try to touch other collaborators we haven't
            # stubbed.
            raise _Reached("policy path entered")

    class _Reached(Exception):
        pass

    sup._msg_store = _MsgStore()

    # Force the policy lookup to return a recommendation so
    # append_event fires immediately. ``_policy_recommendation`` is the
    # first thing past the pause guard.
    class _Rec:
        action = "nudge"
        reason = "test"

    sup._policy_recommendation = lambda *_a, **_kw: _Rec()  # type: ignore[method-assign]

    class _Policy:
        name = "default"

    sup._recovery_policy = _Policy()

    with pytest.raises(_Reached):
        sup.maybe_recover_session(
            launch, failure_type="missing_window",
            failure_message="window missing",
        )
    # Confirms the apply path was entered (append_event called).
    assert reached_policy


def test_supervisor_restart_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Direct relaunch facade yields before lease / tmux side effects."""
    from pollypm.supervisor import Supervisor

    _write_marker(config_with_base_dir, ["operator"])

    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "operator"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))
    store = _FakeStore()
    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir
    sup._msg_store = store
    sup._launch_by_session = lambda _name: launch  # type: ignore[method-assign]
    sup._assert_lease_available = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lease check should not run when paused")
        )
    )

    sup.restart_session(
        "operator", "claude_primary", failure_type="missing_window",
    )

    assert len(store.kw_events) == 1
    event = store.kw_events[0]
    assert event["sender"] == "session.pause.skip"
    assert event["payload"]["loop"] == "supervisor.restart_session"


def test_supervisor_launch_session_skips_unreadable_marker(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Unreadable marker suppresses launch/window creation fail-closed."""
    from pollypm.supervisor import Supervisor

    marker = config_with_base_dir.project.base_dir / "paused-sessions.json"
    marker.write_text("{not json}")

    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "operator"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))
    store = _FakeStore()
    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir
    sup._msg_store = store
    sup._launch_by_session = lambda _name: launch  # type: ignore[method-assign]
    sup._window_map = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("window creation should not inspect tmux when paused")
    )

    result = sup.launch_session("operator")

    assert result is launch
    assert len(store.kw_events) == 1
    event = store.kw_events[0]
    assert event["sender"] == "session.pause.skip"
    assert event["payload"]["loop"] == "supervisor.create_session_window"


def test_heartbeat_process_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Heartbeat per-session processing yields before observations/recovery."""
    from pollypm.heartbeats.base import HeartbeatSessionContext
    from pollypm.heartbeats.local import LocalHeartbeatBackend

    _write_marker(config_with_base_dir, ["operator"])
    store = _FakeStore()

    class _Supervisor:
        config = config_with_base_dir
        msg_store = store

        def get_session_runtime(self, _session_name: str) -> None:
            return None

    class _Api:
        supervisor = _Supervisor()

        def record_observation(self, _context) -> None:  # noqa: ANN001
            raise AssertionError("heartbeat observation should be skipped")

        def recover_session(
            self,
            *_args,
            **_kwargs,
        ) -> None:
            raise AssertionError("heartbeat recovery should be skipped")

    context = HeartbeatSessionContext(
        session_name="operator",
        role="operator-pm",
        project_key="demo",
        provider="claude",
        account_name="claude_primary",
        cwd=str(config_with_base_dir.project.root_dir),
        tmux_session="pollypm-storage-closet",
        window_name="operator",
        source_path="",
        source_bytes=0,
        transcript_delta="",
        pane_text="",
        snapshot_path=None,
        snapshot_hash="",
        pane_id=None,
        pane_command=None,
        pane_dead=False,
        window_present=True,
        previous_log_bytes=None,
        previous_snapshot_hash=None,
    )

    LocalHeartbeatBackend()._process_session(_Api(), context)

    assert len(store.kw_events) == 1
    event = store.kw_events[0]
    assert event["sender"] == "session.pause.skip"
    assert event["payload"]["loop"] == "heartbeat.local.process_session"
