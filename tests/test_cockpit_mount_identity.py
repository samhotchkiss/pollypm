"""Regression tests for the cockpit mount-identity binding (Issue #4).

Persona binding background
==========================

For 5+ revisions, clicking "Polly" in the cockpit rail would
sometimes mount Russell's pane (or any persona). Every prior fix
addressed a downstream symptom (role banner, persona-drift detection,
post-hoc alerts). The actual structural cause was in
``cockpit_rail._mounted_session_name``: when ``cockpit_state.json``
had no ``mounted_session`` field, a CWD-fallback would *guess* by
priority (operator > reviewer > worker) and **write the guess back as
ground truth**. The next click on "polly" would see
``state["mounted_session"] == "operator"`` and short-circuit, never
verifying the actual pane content.

These tests pin the new identity-record contract and the verification
logic so that regression cannot return.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from pollypm.cockpit_mount_identity import (
    ExpectedIdentity,
    MountedIdentity,
    expected_identity_for_rail_key,
    expected_identity_for_session_name,
    identity_matches,
    make_mounted_identity,
    verify_mount_against_tmux,
)


def _config(*sessions) -> SimpleNamespace:
    """Build a minimal config with the given session shape."""
    session_map = {s["name"]: SimpleNamespace(**s) for s in sessions}
    return SimpleNamespace(sessions=session_map)


def _operator_session(role: str = "operator-pm") -> dict:
    return {"name": "operator", "role": role, "window_name": "pm-operator"}


def _reviewer_session(role: str = "reviewer") -> dict:
    return {"name": "reviewer", "role": role, "window_name": "pm-reviewer"}


# ---------------------------------------------------------------------------
# Expected identity resolution
# ---------------------------------------------------------------------------


def test_expected_identity_for_polly_rail_key() -> None:
    cfg = _config(_operator_session(), _reviewer_session())
    expected = expected_identity_for_rail_key("polly", cfg)
    assert expected == ExpectedIdentity(
        rail_key="polly",
        session_name="operator",
        role="operator-pm",
        expected_window_name="pm-operator",
    )


def test_expected_identity_for_russell_rail_key() -> None:
    cfg = _config(_operator_session(), _reviewer_session())
    expected = expected_identity_for_rail_key("russell", cfg)
    assert expected == ExpectedIdentity(
        rail_key="russell",
        session_name="reviewer",
        role="reviewer",
        expected_window_name="pm-reviewer",
    )


def test_expected_identity_unknown_rail_key_returns_none() -> None:
    cfg = _config(_operator_session(), _reviewer_session())
    assert expected_identity_for_rail_key("inbox", cfg) is None
    assert expected_identity_for_rail_key("project:demo", cfg) is None


def test_expected_identity_missing_session_returns_none() -> None:
    """Rail key resolves to a session_name, but that session isn't in
    config — should return None rather than guessing."""
    cfg = _config(_reviewer_session())  # operator missing
    assert expected_identity_for_rail_key("polly", cfg) is None


def test_expected_identity_for_session_name_direct_lookup() -> None:
    cfg = _config(_operator_session())
    expected = expected_identity_for_session_name("operator", cfg)
    assert expected is not None
    assert expected.session_name == "operator"
    assert expected.role == "operator-pm"
    assert expected.rail_key == "polly"  # reverse-mapped


# ---------------------------------------------------------------------------
# identity_matches — the static (no tmux) gate
# ---------------------------------------------------------------------------


def test_identity_matches_same_session_role_window() -> None:
    expected = ExpectedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
    )
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=2, mounted_at="2026-04-26T15:00:00+00:00",
    )
    assert identity_matches(mounted, expected)


def test_identity_matches_rejects_different_session_name() -> None:
    """The persona-binding bug: ``mounted_session == 'operator'`` but
    actual identity is reviewer. ``identity_matches`` must catch the
    role mismatch even when the session_name happens to align."""
    expected = ExpectedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
    )
    # Persisted mount claims session_name=operator BUT role=reviewer
    # — this is the kind of inconsistency a CWD fallback used to
    # silently produce.
    impostor = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="reviewer",  # WRONG role
        expected_window_name="pm-reviewer",  # and wrong window
        right_pane_id="%5", window_index=2, mounted_at="2026-04-26T15:00:00+00:00",
    )
    assert not identity_matches(impostor, expected)


def test_identity_matches_rejects_renamed_window() -> None:
    """Window name diverged from expected — config edit, manual rename,
    etc. Must invalidate so the caller remounts."""
    expected = ExpectedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
    )
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator-OLD",
        right_pane_id="%5", window_index=2, mounted_at="2026-04-26T15:00:00+00:00",
    )
    assert not identity_matches(mounted, expected)


# ---------------------------------------------------------------------------
# verify_mount_against_tmux — the dynamic (tmux ground truth) gate
# ---------------------------------------------------------------------------


def _pane(pane_id: str, cmd: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(pane_id=pane_id, pane_current_command=cmd)


def _window(name: str, index: int) -> SimpleNamespace:
    return SimpleNamespace(name=name, index=index)


def test_verify_against_tmux_passes_for_consistent_state() -> None:
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%4", "tmux"), _pane("%5", "claude")]
    storage_windows = [_window("pm-operator", 3), _window("pm-reviewer", 4)]
    assert verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


def test_verify_against_tmux_fails_when_pane_killed() -> None:
    """The recorded pane id is gone — caller must remount."""
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%4", "tmux")]  # %5 is gone
    storage_windows = [_window("pm-operator", 3)]
    assert not verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


def test_verify_against_tmux_fails_when_window_renamed() -> None:
    """Storage closet window expected to be ``pm-operator`` is gone
    (renamed or never spawned). Caller must remount."""
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%5", "claude")]
    storage_windows = [_window("pm-operator-renamed", 3)]
    assert not verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


def test_verify_against_tmux_fails_when_window_reindexed() -> None:
    """Window name still matches but it's at a different index — tmux
    reindexed after another window was killed. The recorded mount no
    longer corresponds to the right window number."""
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%5", "claude")]
    storage_windows = [_window("pm-operator", 5)]  # was 3, now 5
    assert not verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


def test_verify_against_tmux_fails_when_pane_is_dead_shell() -> None:
    """Pane exists at the right id but is no longer a live provider —
    the agent died and the pane was respawned as a shell."""
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%5", "bash")]  # not claude/codex/node
    storage_windows = [_window("pm-operator", 3)]
    assert not verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


def test_verify_against_tmux_tolerates_version_string_command() -> None:
    """Claude Code sometimes reports its version as the command name
    (e.g. ``2.1.98`` instead of ``claude``). Accept that as a live
    provider."""
    mounted = MountedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
        right_pane_id="%5", window_index=3, mounted_at="2026-04-26T15:00:00+00:00",
    )
    panes = [_pane("%5", "2.1.98")]
    storage_windows = [_window("pm-operator", 3)]
    assert verify_mount_against_tmux(
        mounted, panes=panes, storage_windows=storage_windows,
    )


# ---------------------------------------------------------------------------
# State-dict round-trip
# ---------------------------------------------------------------------------


def test_mounted_identity_round_trips_through_state_dict() -> None:
    expected = ExpectedIdentity(
        rail_key="polly", session_name="operator",
        role="operator-pm", expected_window_name="pm-operator",
    )
    mounted = make_mounted_identity(
        expected,
        right_pane_id="%5",
        window_index=3,
        now=datetime(2026, 4, 26, 15, 0, 0, tzinfo=UTC),
    )
    state_dict = mounted.to_state_dict()
    rehydrated = MountedIdentity.from_state_dict(state_dict)
    assert rehydrated == mounted


def test_mounted_identity_from_state_dict_returns_none_on_garbage() -> None:
    """Defensive: corrupt persisted state must not crash the cockpit
    boot. Caller treats ``None`` as 'no mount on record' and remounts."""
    assert MountedIdentity.from_state_dict(None) is None
    assert MountedIdentity.from_state_dict("not a dict") is None
    assert MountedIdentity.from_state_dict({"missing": "fields"}) is None
    assert MountedIdentity.from_state_dict(
        {"session_name": "operator"}  # incomplete
    ) is None
