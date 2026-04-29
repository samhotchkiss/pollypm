"""Tests for the actionability-based signal routing policy (#883)."""

from __future__ import annotations

import pytest

from pollypm.signal_routing import (
    AlertChannel,
    ROUTED_EMITTERS,
    RoutingDecision,
    SignalActionability,
    SignalAudience,
    SignalEnvelope,
    SignalSeverity,
    SignalSurface,
    alert_channel,
    alert_should_toast,
    compute_dedupe_key,
    is_operational_alert,
    missing_routed_emitters,
    register_routed_emitter,
    required_high_traffic_emitters,
    route_signal,
    shared_alert_count,
    shared_inbox_count,
)


# ---------------------------------------------------------------------------
# Factory helpers for tidy tests
# ---------------------------------------------------------------------------


def _envelope(
    *,
    audience: SignalAudience = SignalAudience.USER,
    severity: SignalSeverity = SignalSeverity.WARN,
    actionability: SignalActionability = SignalActionability.ACTION_REQUIRED,
    source: str = "test",
    subject: str = "Subject",
    body: str = "Body",
    project: str | None = None,
    dedupe_key: str | None = None,
    suggested_action: str | None = None,
) -> SignalEnvelope:
    return SignalEnvelope(
        audience=audience,
        severity=severity,
        actionability=actionability,
        source=source,
        subject=subject,
        body=body,
        project=project,
        dedupe_key=dedupe_key,
        suggested_action=suggested_action,
    )


# ---------------------------------------------------------------------------
# route_signal — the core policy
# ---------------------------------------------------------------------------


def test_dev_audience_drops_completely() -> None:
    """Dev / synthetic audience must never reach a live surface.

    The audit cites the recurrence: synthetic events polluting
    live signal. Tagging audience makes the filter trivial."""
    decision = route_signal(_envelope(audience=SignalAudience.DEV))
    assert decision.surfaces == frozenset()
    assert "dropped" in decision.reason.lower()


def test_operator_audience_routes_to_activity_only() -> None:
    """Operator-only audience: forensic visibility, no interrupt."""
    decision = route_signal(
        _envelope(
            audience=SignalAudience.OPERATOR,
            actionability=SignalActionability.ACTION_REQUIRED,
        )
    )
    assert decision.surfaces == frozenset({SignalSurface.ACTIVITY})


def test_operational_actionability_routes_to_activity_only() -> None:
    """Operational signal regardless of audience → Activity only.

    This is the #765 rule: heartbeat classification is operational
    until remediation fails. Toasting it trains the user to
    dismiss alerts wholesale."""
    decision = route_signal(
        _envelope(
            audience=SignalAudience.USER,
            actionability=SignalActionability.OPERATIONAL,
        )
    )
    assert decision.surfaces == frozenset({SignalSurface.ACTIVITY})
    assert "operational" in decision.reason.lower()


def test_informational_routes_to_activity_and_inbox() -> None:
    """Informational: discoverable but not interrupting.

    No toast, no rail badge, no Home Action-Needed card."""
    decision = route_signal(
        _envelope(actionability=SignalActionability.INFORMATIONAL)
    )
    assert decision.surfaces == frozenset(
        {SignalSurface.ACTIVITY, SignalSurface.INBOX}
    )
    assert SignalSurface.TOAST not in decision.surfaces
    assert SignalSurface.RAIL not in decision.surfaces


def test_action_required_user_routes_everywhere() -> None:
    """Action-required + user audience → full delivery.

    This is the only path that mounts a toast."""
    decision = route_signal(
        _envelope(
            audience=SignalAudience.USER,
            actionability=SignalActionability.ACTION_REQUIRED,
        )
    )
    assert decision.surfaces == frozenset(
        {
            SignalSurface.ACTIVITY,
            SignalSurface.INBOX,
            SignalSurface.RAIL,
            SignalSurface.HOME,
            SignalSurface.TOAST,
        }
    )


def test_route_signal_is_pure() -> None:
    """The same envelope produces the same decision twice in a row."""
    env = _envelope(actionability=SignalActionability.ACTION_REQUIRED)
    a = route_signal(env)
    b = route_signal(env)
    assert a == b


def test_routing_decision_is_immutable() -> None:
    """``RoutingDecision`` is frozen so callers cannot mutate the
    canonical answer they got back."""
    decision = route_signal(_envelope())
    with pytest.raises((AttributeError, TypeError)):
        decision.surfaces = frozenset()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Envelope serialization
# ---------------------------------------------------------------------------


def test_envelope_to_dict_includes_all_fields() -> None:
    """The dict form must round-trip every field for storage."""
    env = _envelope(
        suggested_action="Open Tasks and claim demo/5",
        dedupe_key="work:plan_ready:demo/5",
        project="demo",
    )
    payload = env.to_dict()
    assert payload["audience"] == "user"
    assert payload["actionability"] == "action_required"
    assert payload["source"] == "test"
    assert payload["suggested_action"] == "Open Tasks and claim demo/5"
    assert payload["dedupe_key"] == "work:plan_ready:demo/5"
    assert payload["project"] == "demo"


def test_signal_envelope_replaces_cli_suggested_action() -> None:
    env = _envelope(suggested_action="pm task claim demo/5")

    assert env.suggested_action == "Open Polly and use the relevant cockpit action."
    assert env.to_dict()["suggested_action"] == (
        "Open Polly and use the relevant cockpit action."
    )


# ---------------------------------------------------------------------------
# compute_dedupe_key
# ---------------------------------------------------------------------------


def test_dedupe_key_is_stable() -> None:
    """Same inputs produce the same key."""
    a = compute_dedupe_key(source="hb", kind="no_session", target="demo/5")
    b = compute_dedupe_key(source="hb", kind="no_session", target="demo/5")
    assert a == b


def test_dedupe_key_distinguishes_targets() -> None:
    """Different targets produce different keys so two stuck tasks
    each get their own deduped delivery."""
    a = compute_dedupe_key(source="hb", kind="stuck", target="demo/5")
    b = compute_dedupe_key(source="hb", kind="stuck", target="demo/6")
    assert a != b


def test_dedupe_key_human_readable_when_short() -> None:
    """Short keys keep their human-readable form for debugging."""
    key = compute_dedupe_key(source="hb", kind="no_session", target="demo/5")
    assert key == "hb:no_session:demo/5"


def test_dedupe_key_hashes_when_too_long() -> None:
    """Long keys collapse to a hashed form to keep the column
    width and dedup-set memory bounded."""
    long_target = "x" * 200
    key = compute_dedupe_key(source="hb", kind="huge", target=long_target)
    assert len(key) <= 60
    assert key.startswith("hb:huge:")


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


def test_required_high_traffic_emitters_is_frozen() -> None:
    """The release-gate set is frozen so a forgetful refactor
    cannot quietly drop a required emitter."""
    required = required_high_traffic_emitters()
    assert isinstance(required, frozenset)
    assert "work_service" in required
    assert "supervisor_alerts" in required
    assert "heartbeat" in required


def test_register_routed_emitter_is_idempotent() -> None:
    """Re-registering the same name is a no-op."""
    initial = set(ROUTED_EMITTERS)
    register_routed_emitter("test_emitter_double")
    register_routed_emitter("test_emitter_double")
    assert "test_emitter_double" in ROUTED_EMITTERS
    # No mutations beyond the additive registration.
    delta = ROUTED_EMITTERS - initial
    assert delta == {"test_emitter_double"}


def test_missing_routed_emitters_returns_unmigrated_required_set() -> None:
    """The helper the release gate inspects must return only the
    *required* emitters that haven't migrated yet."""
    # All required emitters have NOT been registered yet (until we
    # migrate them in a follow-up PR), so missing should equal
    # required minus whatever has been registered globally.
    missing = missing_routed_emitters()
    required = required_high_traffic_emitters()
    assert missing.issubset(required)


# ---------------------------------------------------------------------------
# Shared count API
# ---------------------------------------------------------------------------


def test_shared_alert_count_filters_operational_by_default() -> None:
    """The default rail/home reading must filter operational
    alerts. The audit cites #879 — 98 no_session alerts polluting
    the count would have hidden the real action-required state."""
    rows = [
        {"alert_type": "suspected_loop"},
        {"alert_type": "stabilize_failed"},
        {"alert_type": "stuck_on_task:demo/5"},
        {"alert_type": "plan_ready"},
    ]
    # suspected_loop and stabilize_failed are operational; the
    # other two are user-actionable.
    count = shared_alert_count(rows)
    assert count == 2


def test_shared_alert_count_can_include_operational() -> None:
    """Debug surfaces may want every open alert; the helper allows
    opting in explicitly."""
    rows = [
        {"alert_type": "suspected_loop"},
        {"alert_type": "plan_ready"},
    ]
    assert shared_alert_count(rows, include_operational=True) == 2


def test_shared_alert_count_handles_object_rows() -> None:
    """Some store backends return objects, not dicts. Both shapes
    must work — the cockpit cannot care which reader it is."""

    class _Row:
        def __init__(self, alert_type: str) -> None:
            self.alert_type = alert_type

    rows = [_Row("suspected_loop"), _Row("plan_ready")]
    assert shared_alert_count(rows) == 1


def test_shared_alert_count_returns_zero_on_empty() -> None:
    """No alerts → zero count, not error."""
    assert shared_alert_count([]) == 0


def test_shared_inbox_count_returns_zero_on_unwritable_config() -> None:
    """The helper must never raise — a stale 0 is safer than the
    cockpit failing to render."""

    class _BrokenConfig:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("config explosion")

    # Doesn't raise; returns 0.
    assert shared_inbox_count(_BrokenConfig()) == 0


# ---------------------------------------------------------------------------
# Re-exports — callers must have one import path
# ---------------------------------------------------------------------------


def test_alert_channel_is_re_exported() -> None:
    """``signal_routing.alert_channel`` must be the same callable
    as ``cockpit_alerts.alert_channel`` so future migrations have
    one canonical import."""
    from pollypm.cockpit_alerts import alert_channel as canonical
    assert alert_channel is canonical


def test_alert_should_toast_is_re_exported() -> None:
    from pollypm.cockpit_alerts import alert_should_toast as canonical
    assert alert_should_toast is canonical


def test_is_operational_alert_is_re_exported() -> None:
    from pollypm.cockpit_alerts import is_operational_alert as canonical
    assert is_operational_alert is canonical


def test_alert_channel_enum_is_re_exported() -> None:
    """Re-exporting the enum lets new emitters import everything
    they need from one module."""
    from pollypm.cockpit_alerts import AlertChannel as canonical
    assert AlertChannel is canonical
