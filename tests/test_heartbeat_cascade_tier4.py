"""Tests for tier-4 broader-authority cascade (#1553).

Covers:

* :func:`root_cause_hash` — stability and sensitivity.
* :class:`Tier4PromotionTracker` — state transitions (auto-promote,
  self-promote with cooldown, demote-on-clear, demote-on-budget).
* Auto-promotion: 3 tier-3 dispatches → 4th routes to tier-4.
* Self-promotion: cooldown blocks repeat; fresh hash succeeds;
  missing justification rejected.
* System-scoped action emits both ``audit.tier4_global_action`` AND a
  desktop notification (mock the push sink).
* Budget exhaustion: 2h+ at tier-4 → terminal path (mock the inbox
  sink); confirm ``product_state=broken`` is set; confirm urgent
  inbox item is created.
* Tier-3 path unchanged for findings under the auto-promote
  threshold (regression test).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pollypm.audit.log import (
    EVENT_TIER4_BUDGET_EXHAUSTED,
    EVENT_TIER4_DEMOTED,
    EVENT_TIER4_GLOBAL_ACTION,
    EVENT_TIER4_PROMOTED,
    EVENT_WATCHDOG_OPERATOR_DISPATCHED,
    EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED,
    read_events,
)
from pollypm.audit.tier4 import (
    AUTO_PROMOTE_THRESHOLD,
    AUTO_PROMOTE_WINDOW_SECONDS,
    PROMOTION_PATH_SELF,
    PROMOTION_PATH_WATCHDOG,
    SELF_PROMOTE_COOLDOWN_SECONDS,
    TIER4_BUDGET_SECONDS,
    Tier4PromotionTracker,
    Tier4State,
    root_cause_hash,
)
from pollypm.audit.watchdog import (
    Finding,
    RULE_QUEUE_WITHOUT_MOTION,
    RULE_TASK_PROGRESS_STALE,
    TIER_2,
    TIER_3,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return audit_home


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 9, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def tracker_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def tracker(tracker_db: Path) -> Tier4PromotionTracker:
    return Tier4PromotionTracker(tracker_db)


def _finding(
    rule: str = RULE_QUEUE_WITHOUT_MOTION,
    project: str = "demo",
    subject: str = "demo",
    evidence: dict | None = None,
    message: str = "stalled",
) -> Finding:
    return Finding(
        rule=rule,
        project=project,
        subject=subject,
        message=message,
        tier=TIER_2,
        evidence=evidence or {"queued_subjects": ["demo/1"]},
    )


# ---------------------------------------------------------------------------
# root_cause_hash
# ---------------------------------------------------------------------------


def test_root_cause_hash_stable_for_same_finding() -> None:
    a = _finding()
    b = _finding()
    assert root_cause_hash(a) == root_cause_hash(b)


def test_root_cause_hash_changes_with_rule() -> None:
    a = _finding(rule=RULE_QUEUE_WITHOUT_MOTION)
    b = _finding(rule=RULE_TASK_PROGRESS_STALE)
    assert root_cause_hash(a) != root_cause_hash(b)


def test_root_cause_hash_changes_with_project() -> None:
    a = _finding(project="alpha")
    b = _finding(project="beta")
    assert root_cause_hash(a) != root_cause_hash(b)


def test_root_cause_hash_changes_with_evidence() -> None:
    a = _finding(evidence={"queued_subjects": ["demo/1"]})
    b = _finding(evidence={"queued_subjects": ["demo/2"]})
    assert root_cause_hash(a) != root_cause_hash(b)


def test_root_cause_hash_ignores_message_drift() -> None:
    """Cosmetic copy on the finding must not drift the hash."""
    a = _finding(message="version-1 copy")
    b = _finding(message="completely different copy on next release")
    assert root_cause_hash(a) == root_cause_hash(b)


def test_root_cause_hash_stable_under_evidence_key_reorder() -> None:
    """Two equivalent evidence dicts in different key orders hash same."""
    a = _finding(evidence={"a": 1, "b": [1, 2], "c": {"x": 9}})
    b = _finding(evidence={"c": {"x": 9}, "b": [1, 2], "a": 1})
    assert root_cause_hash(a) == root_cause_hash(b)


def test_root_cause_hash_is_short_hex() -> None:
    h = root_cause_hash(_finding())
    assert len(h) == 16
    int(h, 16)  # parses as hex


# ---------------------------------------------------------------------------
# Tier4PromotionTracker — read/write basics
# ---------------------------------------------------------------------------


def test_tracker_get_returns_none_for_unknown_hash(
    tracker: Tier4PromotionTracker,
) -> None:
    assert tracker.get("0" * 16) is None


def test_tracker_record_tier3_dispatch_writes_history(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    rch = root_cause_hash(finding)
    state = tracker.record_tier3_dispatch(finding, now=now)
    assert state.root_cause_hash == rch
    assert len(state.dispatch_history) == 1
    assert state.tier4_active is False


def test_tracker_record_tier3_dispatch_idempotent(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_tier3_dispatch(finding, now=now)
    state = tracker.record_tier3_dispatch(finding, now=now + timedelta(minutes=10))
    assert len(state.dispatch_history) == 2


# ---------------------------------------------------------------------------
# Auto-promotion gate
# ---------------------------------------------------------------------------


def test_should_auto_promote_false_below_threshold(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    for i in range(AUTO_PROMOTE_THRESHOLD - 1):
        tracker.record_tier3_dispatch(
            finding, now=now + timedelta(minutes=i),
        )
    assert tracker.should_auto_promote(finding, now=now) is False


def test_should_auto_promote_true_at_threshold(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    for i in range(AUTO_PROMOTE_THRESHOLD):
        tracker.record_tier3_dispatch(
            finding, now=now + timedelta(minutes=i),
        )
    assert tracker.should_auto_promote(finding, now=now + timedelta(minutes=10)) is True


def test_should_auto_promote_false_outside_window(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    # 3 dispatches but all from > 24h ago.
    far_past = now - timedelta(seconds=AUTO_PROMOTE_WINDOW_SECONDS + 100)
    for i in range(AUTO_PROMOTE_THRESHOLD):
        tracker.record_tier3_dispatch(
            finding, now=far_past + timedelta(minutes=i),
        )
    # Fresh dispatch happens at ``now`` — but the trailing 24h window
    # at ``now`` doesn't include the old ones.
    assert tracker.should_auto_promote(finding, now=now) is False


def test_should_auto_promote_false_when_already_active(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    """If a tier-4 run is already active for the hash, don't re-promote."""
    finding = _finding()
    for i in range(AUTO_PROMOTE_THRESHOLD):
        tracker.record_tier3_dispatch(
            finding, now=now + timedelta(minutes=i),
        )
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    assert tracker.should_auto_promote(finding, now=now + timedelta(minutes=30)) is False


# ---------------------------------------------------------------------------
# Self-promote cooldown
# ---------------------------------------------------------------------------


def test_can_self_promote_fresh_hash(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    allowed, reason = tracker.can_self_promote(finding, now=now)
    assert allowed is True
    assert "no prior" in reason


def test_can_self_promote_blocked_in_cooldown(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_SELF,
        justification="exhausted",
    )
    # 1h later — well inside the 6h cooldown.
    allowed, reason = tracker.can_self_promote(
        finding, now=now + timedelta(hours=1),
    )
    assert allowed is False
    assert "cooldown" in reason


def test_can_self_promote_after_cooldown(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_SELF,
        justification="exhausted",
    )
    later = now + timedelta(seconds=SELF_PROMOTE_COOLDOWN_SECONDS + 60)
    allowed, _ = tracker.can_self_promote(finding, now=later)
    assert allowed is True


def test_watchdog_promotion_does_not_block_self_promotion(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    """A prior auto-promotion (path=watchdog) doesn't gate a self-promotion."""
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    allowed, _ = tracker.can_self_promote(
        finding, now=now + timedelta(minutes=10),
    )
    assert allowed is True


def test_record_promotion_rejects_unknown_path(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    with pytest.raises(ValueError):
        tracker.record_promotion(
            _finding(), now=now, promotion_path="invalid",
        )


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_budget_remaining_returns_none_when_inactive(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    assert tracker.budget_remaining_seconds("nonexistent", now=now) is None


def test_budget_remaining_positive_inside_window(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    rch = root_cause_hash(finding)
    remaining = tracker.budget_remaining_seconds(
        rch, now=now + timedelta(minutes=30),
    )
    assert remaining is not None
    assert remaining > 0
    assert remaining < TIER4_BUDGET_SECONDS


def test_is_budget_exhausted_after_window(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    rch = root_cause_hash(finding)
    assert tracker.is_budget_exhausted(
        rch, now=now + timedelta(seconds=TIER4_BUDGET_SECONDS + 60),
    ) is True


def test_clear_flips_active_to_zero(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    rch = root_cause_hash(finding)
    cleared = tracker.clear(rch, now=now + timedelta(minutes=30))
    assert cleared is True
    state = tracker.get(rch)
    assert state is not None
    assert state.tier4_active is False
    # Idempotent.
    cleared2 = tracker.clear(rch, now=now + timedelta(minutes=30))
    # The row is updated again but tier4_active was already 0 — UPDATE
    # rowcount is still 1 (we touched the row). Either result is fine
    # for idempotence; we assert the state stays cleared.
    state2 = tracker.get(rch)
    assert state2 is not None
    assert state2.tier4_active is False


def test_list_active_filters_inactive_rows(
    tracker: Tier4PromotionTracker, now: datetime,
) -> None:
    f1 = _finding(project="alpha")
    f2 = _finding(project="beta")
    tracker.record_promotion(f1, now=now, promotion_path=PROMOTION_PATH_WATCHDOG)
    tracker.record_promotion(f2, now=now, promotion_path=PROMOTION_PATH_WATCHDOG)
    tracker.clear(root_cause_hash(f1), now=now + timedelta(minutes=10))
    active = tracker.list_active()
    assert len(active) == 1
    assert active[0].project == "beta"


# ---------------------------------------------------------------------------
# Auto-promotion routing through cadence handler
# ---------------------------------------------------------------------------


def _patch_workspace_db(
    monkeypatch: pytest.MonkeyPatch, db_path: Path,
) -> None:
    """Make ``_resolve_workspace_state_db_path`` return our tmp DB."""
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence
    monkeypatch.setattr(
        cadence, "_resolve_workspace_state_db_path", lambda: db_path,
    )


def test_auto_promote_routes_fourth_dispatch_to_tier4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    """3 tier-3 dispatches recorded → next call routes to tier-4."""
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)
    # Build a tracker pre-populated with 3 dispatches.
    tracker = Tier4PromotionTracker(db_path)
    finding = _finding()
    for i in range(AUTO_PROMOTE_THRESHOLD):
        tracker.record_tier3_dispatch(
            finding, now=now + timedelta(minutes=i),
        )
    # Stub the inbox sink so we observe the dispatch without touching
    # a real workspace DB.
    captured: dict[str, Any] = {}

    def _stub_create_tier4(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "demo/T4-1"

    monkeypatch.setattr(
        cadence, "_create_operator_tier4_inbox_task", _stub_create_tier4,
    )

    outcome = cadence._dispatch_to_operator_tier4(
        finding,
        project_path=None,
        now=now + timedelta(minutes=5),
        promotion_path=PROMOTION_PATH_WATCHDOG,
        tracker=tracker,
    )
    assert outcome == "dispatched"
    # Tier-4 body must include the authority section + runtime block.
    assert "TIER 4 BROADER AUTHORITY DISPATCH" in captured["body"]
    assert "<tier4_runtime>" in captured["body"]
    assert "<tier4_authority>" in captured["body"]

    # Audit log carries both promoted + dispatched events.
    promoted = read_events(
        finding.project, event=EVENT_TIER4_PROMOTED,
    )
    dispatched = read_events(
        finding.project, event=EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED,
    )
    assert len(promoted) >= 1
    assert len(dispatched) >= 1
    rch = root_cause_hash(finding)
    assert any(
        (e.metadata or {}).get("root_cause_hash") == rch for e in promoted
    )

    # Tier-4 row is now active.
    state = tracker.get(rch)
    assert state is not None
    assert state.tier4_active is True
    assert state.promotion_path == PROMOTION_PATH_WATCHDOG


def test_auto_promote_throttled_when_tier4_already_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)
    tracker = Tier4PromotionTracker(db_path)
    finding = _finding()
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    monkeypatch.setattr(
        cadence, "_create_operator_tier4_inbox_task",
        lambda **_kw: "demo/T4-NEVER",
    )
    outcome = cadence._dispatch_to_operator_tier4(
        finding,
        project_path=None,
        now=now + timedelta(minutes=10),
        promotion_path=PROMOTION_PATH_WATCHDOG,
        tracker=tracker,
    )
    assert outcome == "throttled"


# ---------------------------------------------------------------------------
# Tier-3 path unchanged below threshold (regression)
# ---------------------------------------------------------------------------


def test_tier3_unchanged_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    """A queue_without_motion finding under threshold goes to tier-3."""
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)

    captured: dict[str, Any] = {}

    def _stub_create_tier3(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "demo/T3-1"

    monkeypatch.setattr(
        cadence, "_create_operator_inbox_task", _stub_create_tier3,
    )
    # Make sure the tier-4 path isn't reached — pin the sink so a
    # mistaken call would crash the test.
    monkeypatch.setattr(
        cadence, "_create_operator_tier4_inbox_task",
        lambda **_kw: pytest.fail(
            "tier-4 path was invoked for an under-threshold finding"
        ),
    )

    finding = _finding()
    outcome = cadence._maybe_dispatch_to_operator(
        finding, project_path=None, now=now,
    )
    assert outcome == "dispatched"
    # The tier-3 body must NOT carry the tier-4 authority section.
    assert "TIER 4 BROADER AUTHORITY DISPATCH" not in captured["body"]
    # Audit-log carries the tier-3 event, not the tier-4 event.
    rows = read_events(finding.project, event=EVENT_WATCHDOG_OPERATOR_DISPATCHED)
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Self-promotion CLI surface
# ---------------------------------------------------------------------------


def test_self_promote_cli_rejects_empty_justification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from pollypm.cli_features.tier4 import tier4_app

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        "pollypm.work.cli._resolve_db_path",
        lambda *_a, **_kw: db_path,
    )
    monkeypatch.setattr(
        "pollypm.cli_features.tier4._build_tracker",
        lambda: Tier4PromotionTracker(db_path),
    )

    runner = CliRunner()
    result = runner.invoke(
        tier4_app,
        [
            "self-promote",
            "--rule", RULE_QUEUE_WITHOUT_MOTION,
            "--project", "demo",
            "--subject", "demo",
            "--justification", "   ",
        ],
    )
    assert result.exit_code == 2


def test_self_promote_cli_dry_run_prints_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from pollypm.cli_features.tier4 import tier4_app

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        "pollypm.cli_features.tier4._build_tracker",
        lambda: Tier4PromotionTracker(db_path),
    )

    runner = CliRunner()
    result = runner.invoke(
        tier4_app,
        [
            "self-promote",
            "--rule", RULE_QUEUE_WITHOUT_MOTION,
            "--project", "demo",
            "--subject", "demo",
            "--justification", "exhausted normal-mode options",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "would self-promote" in result.stdout


def test_self_promote_cli_blocked_inside_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    from typer.testing import CliRunner
    from pollypm.cli_features.tier4 import tier4_app

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        "pollypm.cli_features.tier4._build_tracker",
        lambda: Tier4PromotionTracker(db_path),
    )

    # Pre-populate a self-promotion so the cooldown is active.
    tracker = Tier4PromotionTracker(db_path)
    finding = _finding(evidence={"queued_subjects": ["demo/1"]})
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_SELF,
        justification="prior",
    )

    runner = CliRunner()
    result = runner.invoke(
        tier4_app,
        [
            "self-promote",
            "--rule", RULE_QUEUE_WITHOUT_MOTION,
            "--project", "demo",
            "--subject", "demo",
            "--evidence-json", '{"queued_subjects": ["demo/1"]}',
            "--justification", "second attempt within cooldown",
            "--dry-run",
        ],
    )
    assert result.exit_code == 4
    assert "cooldown" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Tier-4 system-scoped action: audit + push notification
# ---------------------------------------------------------------------------


def test_emit_tier4_global_action_writes_audit_and_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    pushed: list[tuple[str, str]] = []

    class _StubNotifier:
        name = "stub"

        def is_available(self) -> bool:
            return True

        def notify(self, *, title: str, body: str) -> None:
            pushed.append((title, body))

    # Mock the desktop-notifier import path used inside the helper.
    import pollypm.error_notifications as enmod
    monkeypatch.setattr(enmod, "MacOsDesktopNotifier", _StubNotifier)
    monkeypatch.setattr(enmod, "LinuxNotifySendNotifier", _StubNotifier)

    delivered = cadence.emit_tier4_global_action(
        action="restart-heartbeat",
        root_cause_hash_value="abc1234567890def",
        actor="polly",
        project="demo",
        push_title="Tier-4 bounce",
        push_body="heartbeat bounced",
    )
    assert delivered is True
    assert pushed and pushed[0][0] == "Tier-4 bounce"

    rows = read_events("demo", event=EVENT_TIER4_GLOBAL_ACTION)
    assert len(rows) >= 1
    assert (rows[0].metadata or {}).get("action") == "restart-heartbeat"
    assert (rows[0].metadata or {}).get("scope") == "system"


def test_emit_tier4_action_writes_project_scoped_audit() -> None:
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        emit_tier4_action,
    )
    from pollypm.audit.log import EVENT_TIER4_ACTION

    emit_tier4_action(
        project="demo",
        action="recreate-stuck-task",
        root_cause_hash_value="abc1234567890def",
        actor="polly",
        metadata={"task_id": "demo/7"},
    )
    rows = read_events("demo", event=EVENT_TIER4_ACTION)
    assert len(rows) >= 1
    assert (rows[0].metadata or {}).get("action") == "recreate-stuck-task"
    assert (rows[0].metadata or {}).get("task_id") == "demo/7"


# ---------------------------------------------------------------------------
# Budget exhaustion → terminal path
# ---------------------------------------------------------------------------


class _StubMessageStore:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def enqueue_message(self, **kwargs: Any) -> int:
        self.messages.append(kwargs)
        return len(self.messages)


class _StubServices:
    def __init__(self, db_path: Path) -> None:
        from pollypm.storage.state import StateStore
        self.state_store = StateStore(db_path)
        self.msg_store = _StubMessageStore()
        self.known_projects = ()
        self.storage_closet_name = "polly-storage"

    def close(self) -> None:
        try:
            self.state_store.close()
        except Exception:  # noqa: BLE001
            pass


def test_budget_exhaustion_routes_to_terminal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence
    from pollypm.storage.product_state import is_product_broken

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)

    # Promote a finding → fast-forward past the budget.
    tracker = Tier4PromotionTracker(db_path)
    finding = _finding(project="coffeeboardnm", subject="coffeeboardnm")
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    rch = root_cause_hash(finding)

    services = _StubServices(db_path)
    later = now + timedelta(seconds=TIER4_BUDGET_SECONDS + 600)
    counters = cadence._sweep_tier4_budget_and_demotion(
        services=services, now=later,
    )
    services.close()

    assert counters["tier4_budget_exhausted"] >= 1

    # Audit log carries the budget-exhausted row.
    rows = read_events(
        finding.project, event=EVENT_TIER4_BUDGET_EXHAUSTED,
    )
    assert len(rows) >= 1
    assert (rows[0].metadata or {}).get("root_cause_hash") == rch

    # product_state was set to broken.
    from pollypm.storage.state import StateStore
    store2 = StateStore(db_path)
    try:
        assert is_product_broken(store2) is not None
    finally:
        store2.close()

    # Urgent inbox row was written via the stub.
    services2 = _StubServices(db_path)
    # Re-open services not necessary — we kept the messages on the
    # original stub. Inspect that.
    msgs = services.msg_store.messages
    services2.close()
    assert len(msgs) >= 1
    # Body opens with the hypothesis paragraph and lists tried/failed.
    body = msgs[0]["body"]
    assert "Tier-4" in body
    assert "Forensics:" in body
    assert "tier4-terminal" in msgs[0]["labels"]
    # Tracker row is now demoted (active=0).
    state = tracker.get(rch)
    assert state is not None
    assert state.tier4_active is False


# ---------------------------------------------------------------------------
# Demotion-on-clear via public helper
# ---------------------------------------------------------------------------


def test_clear_tier4_for_finding_emits_demoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)
    tracker = Tier4PromotionTracker(db_path)
    finding = _finding(project="demo")
    tracker.record_promotion(
        finding, now=now, promotion_path=PROMOTION_PATH_WATCHDOG,
    )
    cleared = cadence.clear_tier4_for_finding(
        finding, now=now + timedelta(minutes=30),
    )
    assert cleared is True
    rows = read_events(finding.project, event=EVENT_TIER4_DEMOTED)
    assert len(rows) >= 1
    assert (rows[0].metadata or {}).get("reason") == "finding_cleared"


def test_clear_tier4_for_finding_noop_when_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, db_path)
    finding = _finding(project="demo")
    cleared = cadence.clear_tier4_for_finding(
        finding, now=now + timedelta(minutes=30),
    )
    assert cleared is False


# ---------------------------------------------------------------------------
# pm system gate
# ---------------------------------------------------------------------------


def test_system_helper_refuses_without_tier4_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from pollypm.cli_features.tier4 import system_app

    monkeypatch.delenv("POLLYPM_TIER4_AUTHORITY", raising=False)
    runner = CliRunner()
    result = runner.invoke(system_app, ["restart-heartbeat"])
    assert result.exit_code == 2
    assert "tier-3 Polly" in (result.stdout + result.stderr)


def test_system_helper_accepts_env_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from pollypm.cli_features.tier4 import system_app
    import pollypm.cli_features.tier4 as tier4_mod

    monkeypatch.setenv("POLLYPM_TIER4_AUTHORITY", "1")
    # Stub out the actual bounce so the test doesn't shell out to tmux.
    monkeypatch.setattr(
        tier4_mod, "_tmux_kill_session", lambda _name: True,
    )
    monkeypatch.setattr(
        tier4_mod, "_resolve_storage_closet_session",
        lambda: "polly-storage",
    )
    # Stub the desktop notifier path.
    import pollypm.plugins_builtin.core_recurring.audit_watchdog as cadence
    monkeypatch.setattr(
        cadence, "_send_tier4_global_action_push",
        lambda **_kw: True,
    )
    runner = CliRunner()
    result = runner.invoke(
        system_app,
        ["restart-heartbeat", "--root-cause-hash", "abc"],
    )
    assert result.exit_code == 0
    assert "restart-heartbeat" in result.stdout
