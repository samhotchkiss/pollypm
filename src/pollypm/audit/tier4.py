"""Tier-4 Polly promotion + budget accounting (#1553).

Born from the heartbeat-cascade interview (2026-05-09): once the same
``root_cause_hash`` has hit tier-3 dispatch K times in W hours, route
the next dispatch to tier-4 Polly (broader authority) instead. Tier-4
operators can also self-promote when they've exhausted normal-mode
options on a finding. Both paths get a wall-clock budget — if tier-4
hasn't resolved the finding inside ``TIER4_BUDGET_SECONDS``, the
cascade falls through to terminal (product-broken + urgent inbox).

This module owns:

* :func:`root_cause_hash` — stable hash over the finding's structural
  signature. Same finding shape → same hash; different rule, project,
  or structured signature → different hash. Crucially, the hash IS NOT
  derived from the human-readable message / recommendation — those are
  copy that drifts; the hash needs to be stable across cosmetic edits
  for the auto-promote counter to count.

* :class:`Tier4PromotionTracker` — sqlite-backed accounting of dispatch
  history + promotion timestamps + tier-4 entry timestamps per
  ``root_cause_hash``. Persisted in the canonical workspace state DB
  (table ``tier4_promotion_state``, schema lives in
  :mod:`pollypm.storage.state`). Idempotent across heartbeat-process
  restarts because the audit log + the table are the source of truth;
  in-memory state isn't load-bearing.

Tunables (numbers from the 2026-05-09 spec; first-pass — change here,
not in callers):

* :data:`AUTO_PROMOTE_THRESHOLD` — K=3 tier-3 dispatches for the same
  hash inside the window auto-promotes the next dispatch.
* :data:`AUTO_PROMOTE_WINDOW_SECONDS` — W=24h sliding window.
* :data:`SELF_PROMOTE_COOLDOWN_SECONDS` — 6h between self-promotions
  for the same hash. Prevents tier-3 Polly from immediately
  re-triggering to access broader authority.
* :data:`TIER4_BUDGET_SECONDS` — 2h wall-clock at tier-4 before the
  cascade falls through to terminal.

Numbers are first-pass per the spec; tune in this module without
touching the dispatchers.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable constants — first-pass values from the 2026-05-09 spec.
# ---------------------------------------------------------------------------

#: K — auto-promote threshold. The Kth tier-3 dispatch for the same
#: root_cause_hash inside the window routes to tier-4 instead.
AUTO_PROMOTE_THRESHOLD = 3
#: W — sliding window (in seconds) the auto-promote threshold reads from.
AUTO_PROMOTE_WINDOW_SECONDS = 24 * 60 * 60
#: Cooldown between self-promotions for the same root_cause_hash.
#: Prevents a tier-3 Polly from immediately re-triggering self-promote
#: to access broader authority.
SELF_PROMOTE_COOLDOWN_SECONDS = 6 * 60 * 60
#: Wall-clock budget at tier-4 before the cascade falls through to the
#: terminal path (product-broken + urgent inbox).
TIER4_BUDGET_SECONDS = 2 * 60 * 60

#: Promotion paths — written into the audit log + tracker rows.
PROMOTION_PATH_WATCHDOG = "watchdog"
PROMOTION_PATH_SELF = "self"


# ---------------------------------------------------------------------------
# root_cause_hash
# ---------------------------------------------------------------------------


def _structured_signature(finding: Any) -> dict[str, Any]:
    """Extract the structural slice of a Finding for hashing.

    A Finding's ``message`` / ``recommendation`` / ``severity`` are
    cosmetic copy that may drift across releases; ``rule`` /
    ``project`` / ``subject`` are stable identifiers; ``evidence`` is
    the structured payload tier-2/3/4 builders consume.

    The hash takes only the stable structural slice. ``evidence`` is
    flattened by sorting top-level keys + JSON-canonicalising the
    nested values so two callers passing the same evidence dict in
    different key orders still produce the same hash.

    Defensive against partial / non-Finding objects: if a field is
    missing we substitute the empty string. Callers must still pass
    something Finding-shaped; the helper is not a parser.
    """
    rule = str(getattr(finding, "rule", "") or "")
    project = str(getattr(finding, "project", "") or "")
    subject = str(getattr(finding, "subject", "") or "")
    evidence_raw = getattr(finding, "evidence", None) or {}
    if not isinstance(evidence_raw, dict):
        evidence_raw = {}
    # Sort + JSON-encode for canonical form. ``default=str`` so we don't
    # crash if the evidence carries datetime / Path values.
    try:
        evidence_json = json.dumps(
            evidence_raw,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        evidence_json = ""
    return {
        "rule": rule,
        "project": project,
        "subject": subject,
        "evidence_json": evidence_json,
    }


def root_cause_hash(finding: Any) -> str:
    """Return a stable 16-hex-char hash of the finding's structural signature.

    Inputs:
        finding: a :class:`pollypm.audit.watchdog.Finding` — only the
            structural fields are hashed. ``message`` / ``recommendation``
            / ``metadata`` / ``severity`` are deliberately excluded
            because they're cosmetic and may drift across releases.

    The hash is short (16 hex chars = 64 bits) — collision resistance
    is fine for the cardinality this tracker handles (a few rows per
    project per day at most). Long enough that auto-promote counters
    don't accidentally collide across unrelated projects.

    Stable across:
        - Different ``message`` / ``recommendation`` text.
        - Different ``metadata`` payloads (free-form telemetry only).
        - Re-ordering of keys inside ``evidence``.

    Sensitive to:
        - Different ``rule``, ``project``, or ``subject``.
        - Any structural change to the ``evidence`` payload.
    """
    sig = _structured_signature(finding)
    canonical = "|".join([
        sig["rule"], sig["project"], sig["subject"], sig["evidence_json"],
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Tier4PromotionTracker
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Tier4State:
    """One ``tier4_promotion_state`` row materialised."""

    root_cause_hash: str
    project: str
    rule: str
    dispatch_history: tuple[datetime, ...]
    last_promotion_at: datetime | None
    promotion_path: str | None
    tier4_entered_at: datetime | None
    tier4_active: bool
    last_finding_signature: str
    updated_at: datetime | None

    def dispatch_count_in_window(
        self, now: datetime, window_seconds: int,
    ) -> int:
        """Count dispatches inside the trailing ``window_seconds``."""
        if not self.dispatch_history:
            return 0
        cutoff = now - timedelta(seconds=window_seconds)
        return sum(1 for ts in self.dispatch_history if ts >= cutoff)


class Tier4PromotionTracker:
    """sqlite-backed tracker for tier-4 promotion + budget accounting.

    Wraps the workspace state DB (``state.db`` + the
    ``tier4_promotion_state`` table). All reads/writes go through here
    so the dispatch path doesn't have to touch raw SQL — and so the
    table layout can change without rippling through callers.

    Construction takes a path because the dispatch path resolves the
    canonical state.db lazily (a heartbeat-process restart picks up a
    new path, etc.). Pass either a string path or :class:`Path`;
    nonexistent files are created on first write via
    :class:`pollypm.storage.state.StateStore` which owns the schema.

    The class is process-safe because every call opens its own
    short-lived StateStore (sqlite WAL handles concurrent readers).
    No long-lived connection state.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        auto_promote_threshold: int = AUTO_PROMOTE_THRESHOLD,
        auto_promote_window_seconds: int = AUTO_PROMOTE_WINDOW_SECONDS,
        self_promote_cooldown_seconds: int = SELF_PROMOTE_COOLDOWN_SECONDS,
        tier4_budget_seconds: int = TIER4_BUDGET_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self.auto_promote_threshold = int(auto_promote_threshold)
        self.auto_promote_window_seconds = int(auto_promote_window_seconds)
        self.self_promote_cooldown_seconds = int(self_promote_cooldown_seconds)
        self.tier4_budget_seconds = int(tier4_budget_seconds)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _open_store(self):
        from pollypm.storage.state import StateStore
        return StateStore(self._db_path)

    def get(self, root_cause_hash: str) -> Tier4State | None:
        """Return the row for ``root_cause_hash`` or ``None`` if absent."""
        if not root_cause_hash:
            return None
        store = self._open_store()
        try:
            row = store.execute(
                """
                SELECT root_cause_hash, project, rule, dispatch_history_json,
                       last_promotion_at, promotion_path, tier4_entered_at,
                       tier4_active, last_finding_signature, updated_at
                FROM tier4_promotion_state
                WHERE root_cause_hash = ?
                """,
                (root_cause_hash,),
            ).fetchone()
        finally:
            store.close()
        if row is None:
            return None
        return _row_to_state(row)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def record_tier3_dispatch(
        self,
        finding: Any,
        *,
        now: datetime,
    ) -> Tier4State:
        """Record one tier-3 dispatch in the sliding-window history.

        Returns the updated Tier4State so callers can branch on the
        post-update dispatch count without a re-read.
        """
        rch = root_cause_hash(finding)
        existing = self.get(rch)
        history = list(existing.dispatch_history) if existing else []
        history.append(_ensure_aware(now))
        # Trim to the window so the JSON blob doesn't grow unbounded
        # for very-noisy long-lived projects.
        history = _trim_history(
            history, now=now, window_seconds=self.auto_promote_window_seconds,
        )
        store = self._open_store()
        try:
            store.execute(
                """
                INSERT INTO tier4_promotion_state (
                    root_cause_hash, project, rule, dispatch_history_json,
                    last_finding_signature, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_cause_hash) DO UPDATE SET
                    project = excluded.project,
                    rule = excluded.rule,
                    dispatch_history_json = excluded.dispatch_history_json,
                    last_finding_signature = excluded.last_finding_signature,
                    updated_at = excluded.updated_at
                """,
                (
                    rch,
                    str(getattr(finding, "project", "") or ""),
                    str(getattr(finding, "rule", "") or ""),
                    json.dumps([_iso(ts) for ts in history]),
                    _signature_blob(finding),
                    _iso(now),
                ),
            )
            store.commit()
        finally:
            store.close()
        return self.get(rch)  # type: ignore[return-value]

    def should_auto_promote(
        self,
        finding: Any,
        *,
        now: datetime,
    ) -> bool:
        """Return True iff the next dispatch should route to tier-4.

        Auto-promote fires when the trailing-window dispatch count is
        already at or above ``auto_promote_threshold``. The convention:
        the caller has already recorded the dispatch via
        :meth:`record_tier3_dispatch` or hasn't yet — both shapes work
        because we count strict ``>=`` and the caller chooses whether
        to count this dispatch in the threshold or not. The recommended
        flow is to call this BEFORE recording, so the Kth dispatch is
        the one that gets promoted (not the K+1th).
        """
        rch = root_cause_hash(finding)
        state = self.get(rch)
        if state is None:
            return False
        # Don't auto-promote if a tier-4 run is already active for this
        # hash — let the existing tier-4 invocation finish (or the
        # budget exhaust) before we issue another promotion.
        if state.tier4_active:
            return False
        count = state.dispatch_count_in_window(
            now, self.auto_promote_window_seconds,
        )
        return count >= self.auto_promote_threshold

    def record_promotion(
        self,
        finding: Any,
        *,
        now: datetime,
        promotion_path: str,
        justification: str = "",
    ) -> Tier4State:
        """Mark ``finding`` as promoted to tier-4 at ``now``.

        Sets ``last_promotion_at``, ``tier4_entered_at`` (only if a
        tier-4 run wasn't already active — re-entry doesn't reset the
        budget), and ``tier4_active=1``.
        """
        if promotion_path not in (PROMOTION_PATH_WATCHDOG, PROMOTION_PATH_SELF):
            raise ValueError(
                f"record_promotion: promotion_path must be "
                f"'{PROMOTION_PATH_WATCHDOG}' or '{PROMOTION_PATH_SELF}', "
                f"got {promotion_path!r}"
            )
        rch = root_cause_hash(finding)
        existing = self.get(rch)
        # Fresh tier-4 entry resets the budget; re-promotion within an
        # active run keeps the original entry timestamp.
        if existing and existing.tier4_active and existing.tier4_entered_at:
            entered_at = existing.tier4_entered_at
        else:
            entered_at = _ensure_aware(now)
        store = self._open_store()
        try:
            store.execute(
                """
                INSERT INTO tier4_promotion_state (
                    root_cause_hash, project, rule, dispatch_history_json,
                    last_promotion_at, promotion_path, tier4_entered_at,
                    tier4_active, last_finding_signature, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(root_cause_hash) DO UPDATE SET
                    project = excluded.project,
                    rule = excluded.rule,
                    last_promotion_at = excluded.last_promotion_at,
                    promotion_path = excluded.promotion_path,
                    tier4_entered_at = excluded.tier4_entered_at,
                    tier4_active = 1,
                    last_finding_signature = excluded.last_finding_signature,
                    updated_at = excluded.updated_at
                """,
                (
                    rch,
                    str(getattr(finding, "project", "") or ""),
                    str(getattr(finding, "rule", "") or ""),
                    json.dumps(
                        [_iso(ts) for ts in (
                            list(existing.dispatch_history) if existing else []
                        )]
                    ),
                    _iso(now),
                    promotion_path,
                    _iso(entered_at),
                    _signature_blob(finding),
                    _iso(now),
                ),
            )
            store.commit()
        finally:
            store.close()
        return self.get(rch)  # type: ignore[return-value]

    def can_self_promote(
        self,
        finding: Any,
        *,
        now: datetime,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a self-promote attempt.

        Cooldown: if the same root_cause_hash had a self-promotion
        recorded inside ``self_promote_cooldown_seconds``, refuse with
        a human-readable reason. Otherwise allow.
        """
        rch = root_cause_hash(finding)
        state = self.get(rch)
        if state is None or state.last_promotion_at is None:
            return (True, "no prior promotion")
        if state.promotion_path != PROMOTION_PATH_SELF:
            # Watchdog auto-promotions don't gate self-promotions.
            return (True, "no prior self-promotion")
        elapsed = _ensure_aware(now) - _ensure_aware(state.last_promotion_at)
        if elapsed.total_seconds() >= self.self_promote_cooldown_seconds:
            return (True, "cooldown elapsed")
        remaining = self.self_promote_cooldown_seconds - int(elapsed.total_seconds())
        return (False, f"cooldown active: {remaining}s remaining")

    def budget_remaining_seconds(
        self,
        root_cause_hash_value: str,
        *,
        now: datetime,
    ) -> int | None:
        """Return remaining tier-4 budget in seconds, or ``None`` if not active."""
        state = self.get(root_cause_hash_value)
        if state is None or not state.tier4_active or state.tier4_entered_at is None:
            return None
        elapsed = _ensure_aware(now) - _ensure_aware(state.tier4_entered_at)
        remaining = self.tier4_budget_seconds - int(elapsed.total_seconds())
        return remaining

    def is_budget_exhausted(
        self,
        root_cause_hash_value: str,
        *,
        now: datetime,
    ) -> bool:
        """True iff a tier-4 run has run past its wall-clock budget."""
        remaining = self.budget_remaining_seconds(
            root_cause_hash_value, now=now,
        )
        if remaining is None:
            return False
        return remaining <= 0

    def clear(
        self,
        root_cause_hash_value: str,
        *,
        now: datetime,
        reason: str = "finding_cleared",
    ) -> bool:
        """Mark tier-4 inactive for ``root_cause_hash_value``.

        Returns True iff a row was updated. The dispatch history +
        last_promotion_at are preserved — only ``tier4_active`` flips
        to 0 and ``updated_at`` is bumped — so forensic reads can still
        see the prior tier-4 run. ``reason`` is recorded in
        ``last_finding_signature`` as ``"cleared:<reason>"`` for the
        post-clear forensic trail.
        """
        if not root_cause_hash_value:
            return False
        store = self._open_store()
        try:
            cur = store.execute(
                """
                UPDATE tier4_promotion_state
                   SET tier4_active = 0,
                       updated_at = ?,
                       last_finding_signature = 'cleared:' || ?
                 WHERE root_cause_hash = ?
                """,
                (_iso(now), reason, root_cause_hash_value),
            )
            store.commit()
            return (cur.rowcount or 0) > 0
        finally:
            store.close()

    def list_active(self) -> list[Tier4State]:
        """Return every row currently flagged ``tier4_active=1``.

        Used by the cadence handler's budget-enforcement sweep so it
        can find every in-flight tier-4 run without scanning the full
        audit log.
        """
        store = self._open_store()
        try:
            rows = store.execute(
                """
                SELECT root_cause_hash, project, rule, dispatch_history_json,
                       last_promotion_at, promotion_path, tier4_entered_at,
                       tier4_active, last_finding_signature, updated_at
                FROM tier4_promotion_state
                WHERE tier4_active = 1
                ORDER BY tier4_entered_at ASC
                """,
            ).fetchall()
        finally:
            store.close()
        return [_row_to_state(r) for r in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_aware(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass-through for tz-aware ones."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return _ensure_aware(dt).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_aware(dt)


def _trim_history(
    history: Sequence[datetime],
    *,
    now: datetime,
    window_seconds: int,
) -> list[datetime]:
    """Drop history entries older than the window so the JSON blob doesn't grow."""
    cutoff = _ensure_aware(now) - timedelta(seconds=max(window_seconds, 0))
    return [_ensure_aware(ts) for ts in history if _ensure_aware(ts) >= cutoff]


def _signature_blob(finding: Any) -> str:
    """Render the structured signature as a short blob for forensic reads.

    NOT the hash itself — this is the rule + subject + evidence-json
    glob the operator can grep when debugging. ``message`` is excluded
    here for the same reason it's excluded from the hash.
    """
    sig = _structured_signature(finding)
    return f"{sig['rule']}|{sig['subject']}|{sig['evidence_json'][:200]}"


def _row_to_state(row: Any) -> Tier4State:
    """Marshal a sqlite row into a Tier4State."""
    history_raw = row[3] or "[]"
    try:
        history_list = json.loads(history_raw)
    except (TypeError, ValueError):
        history_list = []
    history: list[datetime] = []
    for ts in history_list:
        parsed = _parse_iso(ts)
        if parsed is not None:
            history.append(parsed)
    return Tier4State(
        root_cause_hash=str(row[0] or ""),
        project=str(row[1] or ""),
        rule=str(row[2] or ""),
        dispatch_history=tuple(history),
        last_promotion_at=_parse_iso(row[4]),
        promotion_path=row[5] or None,
        tier4_entered_at=_parse_iso(row[6]),
        tier4_active=bool(row[7]),
        last_finding_signature=str(row[8] or ""),
        updated_at=_parse_iso(row[9]),
    )


__all__ = [
    "AUTO_PROMOTE_THRESHOLD",
    "AUTO_PROMOTE_WINDOW_SECONDS",
    "PROMOTION_PATH_SELF",
    "PROMOTION_PATH_WATCHDOG",
    "SELF_PROMOTE_COOLDOWN_SECONDS",
    "TIER4_BUDGET_SECONDS",
    "Tier4PromotionTracker",
    "Tier4State",
    "root_cause_hash",
]
