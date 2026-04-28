"""Tests for mid-flight persona-drift detection (#757)."""

from __future__ import annotations

import pytest

from pollypm.supervisor import detect_persona_drift


# ---------------------------------------------------------------------------
# Positive: drift detected
# ---------------------------------------------------------------------------


def test_drift_detected_on_standing_by_as_wrong_persona() -> None:
    """Classic observed phrasing from the Notesy incident tonight."""
    pane = "Standing by as Russell. Please tell me which of these you want."
    assert detect_persona_drift("operator-pm", pane) == "Russell"


def test_drift_detected_on_holding_as_wrong_persona() -> None:
    pane = "Holding as Russell. Waiting for Sam to confirm."
    assert detect_persona_drift("operator-pm", pane) == "Russell"


def test_drift_detected_on_i_am_wrong_persona() -> None:
    pane = "I am Russell, the code reviewer for this repo."
    assert detect_persona_drift("operator-pm", pane) == "Russell"


def test_drift_detected_on_acting_as_wrong_persona() -> None:
    pane = "Acting as Polly, the project PM for this session."
    assert detect_persona_drift("reviewer", pane) == "Polly"


def test_drift_detected_on_initialized_as_wrong_persona() -> None:
    pane = "Initialized as Russell the code reviewer and waiting for tasks."
    assert detect_persona_drift("architect", pane) == "Russell"


def test_drift_detected_is_case_insensitive() -> None:
    pane = "STANDING BY AS RUSSELL"
    assert detect_persona_drift("operator-pm", pane) == "Russell"


# ---------------------------------------------------------------------------
# Negative: no drift detected
# ---------------------------------------------------------------------------


def test_no_drift_when_pane_empty() -> None:
    assert detect_persona_drift("operator-pm", "") is None


def test_no_drift_when_role_empty() -> None:
    assert detect_persona_drift("", "Standing by as Russell") is None


def test_no_drift_on_casual_mention() -> None:
    """Neutral references to another persona must not trip the detector."""
    pane = "Let me notify Russell about the review. Polly out."
    assert detect_persona_drift("operator-pm", pane) is None


def test_no_drift_when_expected_marker_also_present() -> None:
    """When the session's own marker is visible alongside another, the
    session is legitimately discussing / reviewing the other persona —
    not having an identity crisis."""
    pane = (
        "Polly reviewing Russell's last report.\n"
        "Standing by as Russell — [Russell quoted in transcript]"
    )
    # operator-pm (Polly) is present, so even a "Standing by as
    # Russell" quote doesn't count as drift.
    assert detect_persona_drift("operator-pm", pane) is None


def test_no_drift_on_own_persona_claim() -> None:
    """A session claiming its own identity is fine — no drift."""
    pane = "I am Polly, your operator. Inbox has 3 items."
    assert detect_persona_drift("operator-pm", pane) is None


def test_unknown_role_still_detects_drift_to_known_persona() -> None:
    """Even when the session's own role has no registered marker (e.g.
    worker), the detector still flags when the pane claims a KNOWN
    wrong persona — a worker saying 'Standing by as Russell' is drift
    regardless of whether worker has a canonical marker."""
    pane = "Standing by as Russell"
    # Worker isn't in the marker map (no expected marker), but the
    # pane clearly claims Russell's identity → drift.
    assert detect_persona_drift("worker", pane) == "Russell"


def test_no_drift_on_partial_match() -> None:
    """Loose substring tricks (e.g. 'Russell' embedded in a URL) must
    not trip the detector — the identity-claim patterns are explicit."""
    pane = "Pushed to https://github.com/org/russell-tool/pull/1"
    assert detect_persona_drift("operator-pm", pane) is None


def test_persona_reassertion_message_names_canonical_persona() -> None:
    """#757 — the heartbeat's reactive-remediation message must:
    name the session's canonical persona, name the persona the
    session drifted into (so the model has context), point at the
    operating guide for re-anchoring, and state the explicit ack
    phrase the model should produce so the operator can verify the
    drift cleared on the next snapshot.

    Avoids the ``<system-update>`` tag — prompt-injection defenses
    correctly reject that shape (#755). Uses an owner-tagged plain
    message instead.
    """
    from pollypm.heartbeats.local import (
        _build_persona_reassertion_message,
    )

    msg = _build_persona_reassertion_message(
        role="operator-pm", drifted_to="Russell",
    )

    assert "<system-update>" not in msg  # #755 — never use this shape
    assert "Polly" in msg  # canonical persona for operator-pm
    assert "Russell" in msg  # the persona we drifted INTO
    assert "polly-operator-guide.md" in msg  # operating guide path
    assert "OK Polly" in msg  # ack phrase

    # Reviewer drift to Polly — same shape, mirror role.
    rev_msg = _build_persona_reassertion_message(
        role="reviewer", drifted_to="Polly",
    )
    assert "Russell" in rev_msg
    assert "russell.md" in rev_msg
    assert "OK Russell" in rev_msg


def test_persona_reassertion_architect_omits_invalid_guide_path() -> None:
    """#897 regression — the legacy heartbeat table cited
    ``src/.../architect.md`` but no such file exists. Architect
    persona is built inline (the role contract sets
    ``guide_path=None``), so the remediation message must omit
    the guide line rather than emit a stale path the model might
    hallucinate fill-in content for."""
    from pollypm.heartbeats.local import _build_persona_reassertion_message

    msg = _build_persona_reassertion_message(
        role="architect", drifted_to="Polly",
    )
    # Canonical fields must still be present.
    assert "Archie" in msg
    assert "OK Archie" in msg
    assert "Polly" in msg
    # The invalid guide path must NOT appear.
    assert "architect.md" not in msg
    assert "Operating guide:" not in msg


def test_persona_reassertion_handles_unknown_role() -> None:
    """An unknown role must produce a generic message rather than
    raise — the heartbeat is on the hot path (#897)."""
    from pollypm.heartbeats.local import _build_persona_reassertion_message

    msg = _build_persona_reassertion_message(
        role="planner", drifted_to="Polly",
    )
    assert "planner" in msg
    assert "Polly" in msg
    # Generic fallback never tries to cite a guide.
    assert "Operating guide:" not in msg


def test_persona_reassertion_uses_canonical_role_contract() -> None:
    """#897 — the heartbeat must build remediation through
    role_contract.build_remediation_message so the canonical
    wording lives in one place. Verified by patching the canonical
    helper and confirming the heartbeat picks up the change."""
    import pollypm.heartbeats.local as hb_local

    captured: list[tuple[str, str]] = []

    def _fake(role: str, drifted_to: str) -> str:
        captured.append((role, drifted_to))
        return "FAKE_REMEDIATION"

    real = hb_local._build_canonical_remediation
    hb_local._build_canonical_remediation = _fake
    try:
        out = hb_local._build_persona_reassertion_message(
            role="operator-pm", drifted_to="Russell",
        )
    finally:
        hb_local._build_canonical_remediation = real

    assert out == "FAKE_REMEDIATION"
    assert captured == [("operator-pm", "Russell")]


# ---------------------------------------------------------------------------
# #913 — repo-relative ``src/pollypm/`` paths must not leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role, drifted_to",
    [
        ("operator-pm", "Russell"),
        ("reviewer", "Polly"),
        ("architect", "Polly"),
        ("worker", "Polly"),
        ("heartbeat-supervisor", "Polly"),
    ],
)
def test_remediation_omits_repo_relative_src_path(
    role: str, drifted_to: str,
) -> None:
    """#913 — every persona's heartbeat remediation must avoid the
    literal substring ``src/pollypm/``.

    The original bug: guide-path constants embedded the repo-
    relative form ``src/pollypm/plugins_builtin/...``. That string
    is the source-checkout location, not the install location —
    once PollyPM is installed (editable or wheel) the path no
    longer resolves from the recipient session's cwd. This test
    locks the runtime-emitter contract: regardless of which
    persona is drifting, no message should hand the agent a
    string starting with ``src/pollypm/``."""
    from pollypm.heartbeats.local import _build_persona_reassertion_message

    msg = _build_persona_reassertion_message(role=role, drifted_to=drifted_to)
    assert "src/pollypm/" not in msg, (
        f"persona-drift remediation for role={role!r} still emits a "
        f"repo-relative src/pollypm/ path:\n{msg}"
    )


@pytest.mark.parametrize(
    "role, drifted_to",
    [
        ("operator_pm", "Russell"),
        ("reviewer", "Polly"),
        ("architect", "Polly"),
        ("worker", "Polly"),
        ("heartbeat_supervisor", "Polly"),
    ],
)
def test_canonical_remediation_omits_repo_relative_src_path(
    role: str, drifted_to: str,
) -> None:
    """#913 — same invariant, exercised at the canonical
    ``role_contract.build_remediation_message`` layer (the
    heartbeat delegates to this helper, but other emitters may
    call it directly). Both layers must satisfy the no-``src/`` rule."""
    from pollypm.role_contract import build_remediation_message

    msg = build_remediation_message(role, drifted_to)
    assert "src/pollypm/" not in msg, (
        f"canonical remediation for role={role!r} still emits a "
        f"repo-relative src/pollypm/ path:\n{msg}"
    )


def test_role_contract_guide_paths_resolve_on_disk() -> None:
    """#913 smoke — the new ``importlib.resources``-backed guide
    paths must point at a real file on disk regardless of install
    layout.

    The failure shape this test catches: a typo in
    ``_packaged_guide_path`` or a profile filename rename that
    breaks the lookup silently. Without this check, the
    remediation message would emit a stale leaf filename and the
    runtime resolver (``pm`` / agent reading the file) would 404."""
    from pathlib import Path

    from pollypm.role_contract import ROLE_REGISTRY

    for contract in ROLE_REGISTRY.values():
        if contract.guide_path is None:
            continue
        # Absolute path required — the whole point of the #913 fix
        # is to drop repo-relative shapes.
        path = Path(contract.guide_path)
        assert path.is_absolute(), (
            f"role {contract.key!r}: guide_path is not absolute "
            f"({contract.guide_path!r})"
        )
        assert path.is_file(), (
            f"role {contract.key!r}: guide_path does not resolve to "
            f"a file ({contract.guide_path!r})"
        )
