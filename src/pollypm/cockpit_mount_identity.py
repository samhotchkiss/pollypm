"""Typed records + verification for the cockpit's session mount identity.

Background — the persona-binding bug
====================================

For 5+ revisions, the cockpit had a bug where clicking "Polly" in the
left rail would sometimes mount Russell's pane on the right (or any
other persona). Every prior fix was a band-aid in a different layer
(role banner at kickoff, persona-drift detection in the heartbeat,
post-hoc alerts, etc.). None addressed the structural cause:

    The cockpit had no single source of truth for "what is currently
    mounted" beyond a bare ``mounted_session`` string in
    cockpit_state.json. When that string was empty, a CWD-fallback
    helper would *guess* which session was in the pane — preferring
    ``operator-pm`` over ``reviewer`` over ``worker`` whenever they
    shared a CWD — and then *write the guess back* as ground truth
    (cockpit_rail.py legacy ``_mounted_session_name``). On the next
    click, the early-return optimisation in ``_show_live_session``
    saw "mounted_session == requested_session" and skipped the
    actual mount — leaving the wrong agent visible under the
    requested label.

The fix
=======

Replace the bare string with a typed identity record that captures
*everything* needed to verify a mount is what we think it is:

* ``rail_key``         — what the user clicked ("polly", "russell")
* ``session_name``     — the canonical session config key
* ``role``             — the static role declared in pollypm.toml
* ``expected_window_name`` — the storage-closet window the agent runs in
* ``right_pane_id``    — the tmux pane id we joined into the cockpit
* ``window_index``     — the parent window's index in the storage closet

When the cockpit considers an "already mounted" short-circuit, it now
verifies the mounted identity against tmux ground truth instead of
trusting the persisted string. If any field disagrees with reality
(window renamed, pane killed and re-spawned at a different id, role
changed in config) we tear down and remount fresh rather than papering
over the mismatch.

This module is pure — no tmux IO, no DB. The tmux verification helper
takes a ``tmux`` client as an argument so tests can stub it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pollypm.cockpit_rail_routes import resolve_live_session_route


@dataclass(frozen=True, slots=True)
class ExpectedIdentity:
    """What the user *should* see when a particular rail key is mounted.

    Computed from the rail key + the live config — never from the
    persisted mount state. Used as the comparison baseline when we
    decide whether the current mount is still valid.
    """

    rail_key: str
    session_name: str
    role: str
    expected_window_name: str


@dataclass(frozen=True, slots=True)
class MountedIdentity:
    """Identity of the session currently joined to the cockpit's right pane.

    Persisted in ``cockpit_state.json`` under the ``mounted_identity``
    key. The legacy ``mounted_session`` (bare string) field is kept in
    parallel for backward compatibility — readers should prefer the
    typed record when present.
    """

    rail_key: str
    session_name: str
    role: str
    expected_window_name: str
    right_pane_id: str
    window_index: int | None
    mounted_at: str  # ISO-8601 UTC

    def to_state_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_state_dict(cls, raw: object) -> "MountedIdentity | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                rail_key=str(raw["rail_key"]),
                session_name=str(raw["session_name"]),
                role=str(raw["role"]),
                expected_window_name=str(raw["expected_window_name"]),
                right_pane_id=str(raw["right_pane_id"]),
                window_index=(
                    int(raw["window_index"])
                    if raw.get("window_index") is not None
                    else None
                ),
                mounted_at=str(raw["mounted_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def expected_identity_for_session_name(
    session_name: str, config: object,
) -> ExpectedIdentity | None:
    """Resolve an expected identity from a session_name + the live config.

    Returns ``None`` when the session is unknown or the config is
    malformed — the caller should treat that as "cannot mount" rather
    than guessing.
    """
    sessions = getattr(config, "sessions", {}) or {}
    session_cfg = sessions.get(session_name)
    if session_cfg is None:
        return None
    role = str(getattr(session_cfg, "role", "") or "")
    if not role:
        return None
    window_name = str(
        getattr(session_cfg, "window_name", "") or session_name
    )
    rail_key = _rail_key_for_session_name(session_name)
    return ExpectedIdentity(
        rail_key=rail_key,
        session_name=session_name,
        role=role,
        expected_window_name=window_name,
    )


def expected_identity_for_rail_key(
    rail_key: str, config: object,
) -> ExpectedIdentity | None:
    """Resolve an expected identity from a rail key (e.g. ``"polly"``).

    Returns ``None`` for unknown rail keys (static views, project
    routes, etc.) — those don't mount a live session.
    """
    route = resolve_live_session_route(rail_key)
    if route is None:
        return None
    expected = expected_identity_for_session_name(route.session_name, config)
    if expected is None:
        return None
    # The session_name lookup defaults the rail_key to whatever the
    # reverse map produces; honour the explicit click instead.
    if expected.rail_key == rail_key:
        return expected
    return ExpectedIdentity(
        rail_key=rail_key,
        session_name=expected.session_name,
        role=expected.role,
        expected_window_name=expected.expected_window_name,
    )


def _rail_key_for_session_name(session_name: str) -> str:
    """Best-effort reverse lookup ``session_name -> rail_key``.

    The forward map (rail key -> session_name) lives in
    :mod:`cockpit_rail_routes`. We keep the reverse here as a small
    fallback so a record persisted from ``session_name`` always has
    *some* rail key. Worker/task sessions don't have a rail key —
    they get the session_name verbatim.
    """
    # Hardcoded inverse of _LIVE_SESSION_ROUTES — kept simple to avoid
    # a circular import on the routes module.
    if session_name == "operator":
        return "polly"
    if session_name == "reviewer":
        return "russell"
    return session_name


def make_mounted_identity(
    expected: ExpectedIdentity,
    *,
    right_pane_id: str,
    window_index: int | None,
    now: datetime | None = None,
) -> MountedIdentity:
    """Build the persisted identity record from an expected identity
    and the freshly-joined pane's runtime details."""
    moment = (now or datetime.now(UTC)).isoformat()
    return MountedIdentity(
        rail_key=expected.rail_key,
        session_name=expected.session_name,
        role=expected.role,
        expected_window_name=expected.expected_window_name,
        right_pane_id=right_pane_id,
        window_index=window_index,
        mounted_at=moment,
    )


def identity_matches(
    mounted: MountedIdentity, expected: ExpectedIdentity,
) -> bool:
    """Return True iff the persisted identity is consistent with the
    expected one. Used as the static (no-tmux) gate before the
    expensive tmux verification."""
    return (
        mounted.session_name == expected.session_name
        and mounted.role == expected.role
        and mounted.expected_window_name == expected.expected_window_name
    )


def verify_mount_against_tmux(
    mounted: MountedIdentity,
    *,
    panes: list,  # tmux.list_panes(...) result
    storage_windows: list,  # tmux.list_windows(storage_session) result
) -> bool:
    """Return True iff tmux state matches the mounted identity.

    Three checks, all must pass:

    1. The recorded ``right_pane_id`` still appears in the cockpit
       window's pane list.
    2. The pane's current command looks like a live provider — not a
       dead shell.
    3. The expected storage-closet window with name
       ``expected_window_name`` exists and (if recorded) sits at
       ``window_index``. This catches the case where a window was
       renamed or re-indexed under us.

    Returns False on any mismatch, signalling "tear down and remount".
    """
    # 1. Pane still exists at recorded id?
    pane = next(
        (p for p in panes if getattr(p, "pane_id", None) == mounted.right_pane_id),
        None,
    )
    if pane is None:
        return False

    # 2. Live provider command? (claude / codex / version-string)
    cmd = getattr(pane, "pane_current_command", "") or ""
    if cmd not in {"node", "claude", "codex"}:
        # Tolerate version-string commands (e.g., "2.1.98").
        if not cmd or not all(c.isdigit() or c == "." for c in cmd):
            return False

    # 3. Expected storage-closet window present (and at the recorded
    # index, when one was recorded). The window_index check guards
    # against tmux reindexing after a window was killed earlier in
    # the list.
    window = next(
        (
            w for w in storage_windows
            if getattr(w, "name", None) == mounted.expected_window_name
        ),
        None,
    )
    if window is None:
        return False
    if mounted.window_index is not None:
        actual_index = getattr(window, "index", None)
        if actual_index is not None and actual_index != mounted.window_index:
            return False
    return True
