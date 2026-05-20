"""Leaf module for persona-drift detection (extracted from ``supervisor``).

Owns the persona-marker table, identity-claim phrasings, and the
:func:`detect_persona_drift` predicate used by the heartbeat (#757) to
catch sessions whose pane text shows a different role's identity claim
than the one the session was launched with.

Lives here, not on ``pollypm.supervisor``, so that ``heartbeats.local``
can call :func:`detect_persona_drift` without lazy-importing
``pollypm.supervisor`` (refs #1367). ``pollypm.supervisor`` keeps the
historical re-exports for back-compat with tests and any external
callers.
"""
from __future__ import annotations

# Stable persona markers per role. Used both as the source-of-truth for
# the launch-time persona check (:meth:`Supervisor._schedule_persona_verify`)
# and as a backstop (see ``Supervisor._schedule_persona_verify``) to catch
# (launch, target) tuple crosses where one role's control prompt
# lands in a different role's window. ``worker`` has no stable
# persona and ``triage`` does not currently brand itself, so both
# are intentionally omitted.
_ROLE_PERSONA_MARKER: dict[str, str] = {
    "operator-pm": "Polly",
    "reviewer": "Russell",
    "architect": "Archie",
    "heartbeat-supervisor": "Heartbeat",
}


# Phrasings that unambiguously claim a persona identity — e.g.
# "Standing by as Russell", "I'm Polly", "Holding as Russell". Used
# by :func:`detect_persona_drift` to decide whether the pane shows
# a DIFFERENT persona's identity claim, which is the symptom of a
# mid-flight persona swap (#757). Neutral mentions ("let me notify
# Polly") don't match these patterns.
_IDENTITY_CLAIM_PATTERNS: tuple[str, ...] = (
    "standing by as {marker}",
    "holding as {marker}",
    "i am {marker}",
    "i'm {marker}",
    "continuing as {marker}",
    "acting as {marker}",
    "as {marker}, ",
    "as {marker}.",
    "as {marker},",
    "initialized as {marker}",
)


def detect_persona_drift(role: str, pane_text: str) -> str | None:
    """Return the name of the drifted-to persona, or None on no drift.

    Looks for strong identity-claim phrasings that reference a role
    OTHER than the session's configured role. Conservative by design:
    casual mentions of another persona name don't trip the detector —
    only phrasings the session would use to assert its own identity.

    Used by the heartbeat (#757) to catch sessions that started with
    the correct role but drifted mid-flight — either through kickoff
    clobber (#758) or prompt-injection (#755). Kickoff-time drift is
    caught earlier by :func:`Supervisor._assert_session_launch_matches`.

    Returns the detected persona name (e.g. ``"Russell"``) so callers
    can surface it in the alert message, or ``None`` when no drift is
    observed.
    """
    if not pane_text or not role:
        return None
    expected = _ROLE_PERSONA_MARKER.get(role)
    lowered = pane_text.lower()
    for other_role, other_marker in _ROLE_PERSONA_MARKER.items():
        if other_role == role:
            continue
        # Avoid false positives when the expected marker also appears
        # in the pane — both present means the session is legitimately
        # discussing multiple personas (e.g. Polly reviewing Russell's
        # output).
        if expected and expected.lower() in lowered:
            continue
        for pattern in _IDENTITY_CLAIM_PATTERNS:
            needle = pattern.format(marker=other_marker.lower())
            if needle in lowered:
                return other_marker
    return None
