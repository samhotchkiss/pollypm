"""Actionability-based signal routing and shared count API (#883).

Defines the structured-metadata envelope every cockpit signal
(alert, notification, inbox item, activity event) carries and the
single routing policy that decides which surfaces it lands on.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md`` §3)
identifies the structural problem: events are routed by *caller
preference* — each emitter passes its own opinion of where the
signal should go — instead of by a *shared policy* that knows the
audience, severity, and actionability of the signal. The result is
the recurring pattern of:

* operational events surfaced as user alerts (#765);
* action-required events buried in Activity (#879 — automatic
  recovery paused after rapid failures and 98 ``no_session``
  alerts were visible only on the Activity log);
* the same concept counted differently by Rail and Home (#820 —
  Home counted tracked projects, Rail counted registered
  projects);
* synthetic / test event kinds polluting live signal.

This module is the structural fix. It does not rewrite every
emitter (too risky for v1) — it gives every emitter one envelope
type to populate, one policy function to consult, and one place
the audit asserts is consistent. Existing emitters migrate to it
gradually; new emitters must use it.

Migration policy: the launch-hardening release gate (#889)
inspects ``ROUTED_EMITTERS`` (a registry of modules already
migrated) and blocks v1 if a high-traffic emitter is missing.

Related modules:

* ``cockpit_alerts.py`` — owns the toast tier classification and
  ``AlertChannel`` enum. ``signal_routing`` re-exports
  ``AlertChannel`` and adds the wider envelope around it. The two
  modules are not redundant: ``cockpit_alerts`` is a single
  surface (toasts), ``signal_routing`` is the policy layer above
  every surface.
* ``cockpit_inbox.py`` — owns the inbox count reader.
  ``signal_routing.shared_inbox_count`` delegates to it so callers
  have one import path even if the implementation moves.
* ``events/summaries.py`` — packs activity-feed payload metadata.
  Migration to ``SignalEnvelope`` is one of the highest-value
  follow-ups.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from pollypm.cockpit_alerts import (
    AlertChannel,
    alert_channel,
    alert_should_toast,
    is_operational_alert,
)


__all__ = [
    "AlertChannel",
    "SignalAudience",
    "SignalSeverity",
    "SignalActionability",
    "SignalSurface",
    "SignalEnvelope",
    "RoutingDecision",
    "alert_channel",
    "alert_should_toast",
    "compute_dedupe_key",
    "envelope_for_alert",
    "is_operational_alert",
    "ROUTED_EMITTERS",
    "register_routed_emitter",
    "route_signal",
    "shared_alert_count",
    "shared_inbox_count",
]


# ---------------------------------------------------------------------------
# Enums — the structured metadata every signal carries
# ---------------------------------------------------------------------------


class SignalAudience(enum.Enum):
    """Who the signal is for.

    The audit cites #765: heartbeat classification signals are
    interesting to operators / debugging eyeballs but never to the
    user — yet they were toasted as user alerts. Tagging audience
    explicitly makes the misroute impossible.
    """

    USER = "user"
    """The human cockpit user. Default for anything action-required
    or merely informational (plan ready, task review needed)."""

    OPERATOR = "operator"
    """Operator-only ops noise: heartbeat ticks, supervisor self-
    recovery, plugin lifecycle. Lands on Activity for forensic
    visibility but never interrupts."""

    DEV = "dev"
    """Synthetic / test traffic. Filtered from every live user
    surface by default. Only the dev-channel inbox shows them."""


class SignalSeverity(enum.Enum):
    """How loudly a signal speaks.

    Severity does *not* determine routing — actionability does.
    Severity affects rendering (icon, color, urgency hint) and is
    used by the Activity feed projector.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class SignalActionability(enum.Enum):
    """The user's required response.

    The audit cites #765 and #879 specifically: the policy is
    "operational signals are not interruptions until *remediation
    fails*". Encoding actionability as a first-class field makes
    that policy testable instead of caller-by-caller convention.
    """

    OPERATIONAL = "operational"
    """Internal mechanism noise. Activity log only."""

    INFORMATIONAL = "informational"
    """The user wants to see it eventually but doesn't have to act.
    Activity log + Inbox. No toast."""

    ACTION_REQUIRED = "action_required"
    """The user must do something. Activity + Inbox + Rail badge +
    Toast + Home Action-Needed card."""


class SignalSurface(enum.Enum):
    """A user-visible cockpit surface a signal can land on."""

    ACTIVITY = "activity"
    """The activity feed — every signal lands here for forensic
    visibility, regardless of audience."""

    INBOX = "inbox"
    """The cockpit inbox list."""

    RAIL = "rail"
    """The cockpit rail badge counter."""

    HOME = "home"
    """The Home / Action-Needed card."""

    TOAST = "toast"
    """A transient toast that interrupts the user. Reserved for
    ACTION_REQUIRED with USER audience."""


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SignalEnvelope:
    """The structured metadata every signal must carry.

    Fields:

    * ``audience`` — :class:`SignalAudience`. Required.
    * ``severity`` — :class:`SignalSeverity`. Required.
    * ``actionability`` — :class:`SignalActionability`. Required.
    * ``source`` — short stable identifier of the emitting
      subsystem (e.g., ``"heartbeat"``, ``"work_service"``,
      ``"supervisor"``). Required.
    * ``dedupe_key`` — opaque string that two emissions of the
      same logical signal share. Routing collapses repeated
      emissions of the same key to a single rail/inbox/toast
      delivery — see :func:`compute_dedupe_key` for a stable
      default. Optional but strongly recommended.
    * ``project`` — optional project key to scope the signal.
    * ``subject`` / ``body`` — short rendered text for inbox /
      toast. Required.
    * ``suggested_action`` — optional one-line CLI suggestion
      (e.g., ``"pm task claim demo/5"``). Inbox and Activity
      both render this when present.
    * ``payload`` — free-form structured data carried alongside
      the signal for future inspection. Not required to follow
      any schema.
    """

    audience: SignalAudience
    severity: SignalSeverity
    actionability: SignalActionability
    source: str
    subject: str
    body: str
    project: str | None = None
    dedupe_key: str | None = None
    suggested_action: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly dict for storage / wire transport."""
        return {
            "audience": self.audience.value,
            "severity": self.severity.value,
            "actionability": self.actionability.value,
            "source": self.source,
            "subject": self.subject,
            "body": self.body,
            "project": self.project,
            "dedupe_key": self.dedupe_key,
            "suggested_action": self.suggested_action,
            "payload": dict(self.payload),
        }


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The set of surfaces a signal lands on plus reasoning.

    ``surfaces`` is the canonical answer; ``reason`` is a short
    human-readable string the audit logs use when explaining a
    decision. Keeping the reason on the decision rather than in a
    log means tests can assert on it without parsing log output.
    """

    surfaces: frozenset[SignalSurface]
    reason: str


# Empty result reused for filtered-out signals.
_DROPPED = RoutingDecision(
    surfaces=frozenset(),
    reason="dropped: dev-audience signals never reach live user surfaces",
)


def route_signal(envelope: SignalEnvelope) -> RoutingDecision:
    """Return the set of surfaces ``envelope`` should land on.

    Policy (in order):

    1. ``DEV`` audience → no surface. Synthetic / test traffic
       must never pollute live surfaces; the dev-channel inbox
       reads the underlying store directly.
    2. ``OPERATOR`` audience or ``OPERATIONAL`` actionability
       (regardless of audience) → Activity only.
    3. ``INFORMATIONAL`` actionability → Activity + Inbox.
    4. ``ACTION_REQUIRED`` actionability with ``USER`` audience
       → Activity + Inbox + Rail + Home + Toast.

    The function is pure and total: every envelope produces a
    deterministic decision. Tests can use it without any mock.
    """
    if envelope.audience is SignalAudience.DEV:
        return _DROPPED

    if (
        envelope.audience is SignalAudience.OPERATOR
        or envelope.actionability is SignalActionability.OPERATIONAL
    ):
        return RoutingDecision(
            surfaces=frozenset({SignalSurface.ACTIVITY}),
            reason="operational signal: activity log only (#765)",
        )

    if envelope.actionability is SignalActionability.INFORMATIONAL:
        return RoutingDecision(
            surfaces=frozenset({SignalSurface.ACTIVITY, SignalSurface.INBOX}),
            reason="informational: discoverable but not interrupting",
        )

    # ACTION_REQUIRED + USER audience: full delivery.
    return RoutingDecision(
        surfaces=frozenset(
            {
                SignalSurface.ACTIVITY,
                SignalSurface.INBOX,
                SignalSurface.RAIL,
                SignalSurface.HOME,
                SignalSurface.TOAST,
            }
        ),
        reason="action required: full surface delivery",
    )


# ---------------------------------------------------------------------------
# Dedupe key helper
# ---------------------------------------------------------------------------


_SEVERITY_FROM_ALERT_LEVEL: Mapping[str, SignalSeverity] = {
    "info": SignalSeverity.INFO,
    "warn": SignalSeverity.WARN,
    "warning": SignalSeverity.WARN,
    "error": SignalSeverity.ERROR,
    "critical": SignalSeverity.CRITICAL,
}


def envelope_for_alert(
    *,
    source: str,
    alert_type: str,
    severity_label: str,
    session_name: str,
    subject: str,
    body: str,
    suggested_action: str | None = None,
    project: str | None = None,
) -> SignalEnvelope:
    """Build a :class:`SignalEnvelope` for a legacy ``upsert_alert``-shape
    emission.

    The audit (#894) requires high-traffic emitters (heartbeat,
    supervisor_alerts, work_service) to route through
    :class:`SignalEnvelope`. ``upsert_alert`` callers already carry
    every field this helper needs — this is the bridge that lets a
    site migrate without rewriting its store call.

    The envelope's audience / actionability are derived from the
    existing :func:`alert_channel` classification so
    :func:`route_signal` and :func:`alert_channel` agree by
    construction.
    """
    channel = alert_channel(alert_type)
    if channel is AlertChannel.OPERATIONAL:
        actionability = SignalActionability.OPERATIONAL
        audience = SignalAudience.OPERATOR
    elif channel is AlertChannel.INFORMATIONAL:
        actionability = SignalActionability.INFORMATIONAL
        audience = SignalAudience.USER
    else:
        actionability = SignalActionability.ACTION_REQUIRED
        audience = SignalAudience.USER

    return SignalEnvelope(
        audience=audience,
        severity=_SEVERITY_FROM_ALERT_LEVEL.get(
            (severity_label or "").lower(),
            SignalSeverity.WARN,
        ),
        actionability=actionability,
        source=source,
        subject=subject,
        body=body,
        project=project,
        dedupe_key=compute_dedupe_key(
            source=source,
            kind=alert_type,
            target=session_name,
        ),
        suggested_action=suggested_action,
    )


def compute_dedupe_key(
    *,
    source: str,
    kind: str,
    target: str | None = None,
    extra: str | None = None,
) -> str:
    """Return a stable dedupe key for a signal.

    The audit cites #867: repeated alerts re-fired every heartbeat
    because each emission used a unique id. With a stable key two
    emissions for the same underlying state collapse to one rail /
    inbox / toast delivery.

    Convention: ``<source>:<kind>[:<target>][:<extra>]``. Hashed
    only when the resulting string would be unwieldy (>120 chars)
    so the human-readable form is preserved for debugging when
    short enough.
    """
    parts = [source, kind]
    if target:
        parts.append(target)
    if extra:
        parts.append(extra)
    raw = ":".join(parts)
    if len(raw) <= 120:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{kind}:{digest}"


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


ROUTED_EMITTERS: set[str] = set()
"""The set of subsystem identifiers that have migrated to
:class:`SignalEnvelope` + :func:`route_signal`. Populated at
import time by each migrated emitter via
:func:`register_routed_emitter`.

The release gate (#889) inspects this set and rejects v1 if the
high-traffic emitters (currently: ``"work_service"``,
``"supervisor_alerts"``, ``"heartbeat"``) are missing.
"""


_REQUIRED_HIGH_TRAFFIC_EMITTERS: frozenset[str] = frozenset(
    {
        "work_service",
        "supervisor_alerts",
        "heartbeat",
    }
)


def register_routed_emitter(name: str) -> None:
    """Mark ``name`` as having migrated to the routed envelope.

    Called at module import time by every migrated emitter:

    >>> register_routed_emitter("supervisor_alerts")

    Idempotent.
    """
    ROUTED_EMITTERS.add(name)


def required_high_traffic_emitters() -> frozenset[str]:
    """Return the set the launch-hardening release gate enforces."""
    return _REQUIRED_HIGH_TRAFFIC_EMITTERS


def missing_routed_emitters() -> frozenset[str]:
    """Return the high-traffic emitters that have not yet migrated.

    A clean run returns ``frozenset()``. The audit asserts on this
    in the release gate (#889)."""
    return _REQUIRED_HIGH_TRAFFIC_EMITTERS - ROUTED_EMITTERS


# ---------------------------------------------------------------------------
# Shared count API
# ---------------------------------------------------------------------------


def shared_inbox_count(config: object) -> int:
    """Return the unified inbox count.

    The audit (#820, #799) cites the canonical bug: Home counted
    tracked projects while Rail counted registered projects, and
    the two diverged on screen at the same time. The repo already
    consolidated the implementation into
    ``cockpit_inbox._count_inbox_tasks_for_label``; this re-export
    gives every reader one import path so future moves of the
    underlying function don't fork the call sites again.

    Returns ``0`` on any unexpected error — a stale counter is
    less harmful than the cockpit failing to render at all.
    """
    try:
        from pollypm.cockpit_inbox import _count_inbox_tasks_for_label
    except Exception:  # noqa: BLE001
        return 0
    try:
        return _count_inbox_tasks_for_label(config)
    except Exception:  # noqa: BLE001
        return 0


def shared_alert_count(
    open_alerts: Iterable[object],
    *,
    include_operational: bool = False,
) -> int:
    """Return the unified alert count.

    The audit cites #879: 98 ``no_session`` alerts were visible
    only on the Activity log because the rail badge filtered
    them as operational without notifying the user that
    automatic recovery had paused. The shared count helper makes
    the operational-vs-action-required filter explicit at the
    call site so the rail and Home cannot disagree about it.

    Parameters:

    * ``open_alerts`` — iterable of alert rows. Each row must
      expose either ``.alert_type`` or ``["alert_type"]`` (dict).
    * ``include_operational`` — when ``True``, returns the raw
      count of all open alerts (used by debug surfaces). Default
      ``False`` filters operational alerts out — the only number
      that matters for user-visible badges.
    """
    count = 0
    for row in open_alerts:
        if isinstance(row, Mapping):
            alert_type = row.get("alert_type") or row.get("type") or ""
        else:
            alert_type = getattr(row, "alert_type", None) or getattr(
                row, "type", None
            ) or ""
        if not include_operational and is_operational_alert(str(alert_type)):
            continue
        count += 1
    return count
