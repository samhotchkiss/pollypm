"""Cycle 131 — perf review: bound the alert-notifier dedup set.

``AlertNotifier._seen_alert_ids`` only ever grew. On a long-running
cockpit (days/weeks of churny operational alerts) the set could
accumulate thousands of tuples. The fix: when the set crosses a soft
cap, replace it with the keys for currently-open alerts. Closed
alerts can't re-fire under the same id (each new alert row has a
fresh id), so dropping their keys is safe.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from pollypm.cockpit_alerts import AlertNotifier


def _record(alert_id: int, alert_type: str = "test", session: str = "worker") -> SimpleNamespace:
    return SimpleNamespace(
        alert_id=alert_id,
        alert_type=alert_type,
        session_name=session,
        message="x",
        severity="warn",
        updated_at="2026-04-26T00:00:00+00:00",
    )


def _make_notifier(monkeypatch) -> AlertNotifier:
    """Build a notifier without calling __init__'s container/timer setup."""
    notifier = AlertNotifier.__new__(AlertNotifier)
    notifier.app = SimpleNamespace(set_interval=lambda *_: None)
    notifier.config_path = "/tmp/x"
    notifier.poll_interval = 5.0
    notifier.max_visible = 3
    notifier.bind_a = False
    notifier._seen_alert_ids = set()
    notifier._toasts = []
    notifier._container = None
    notifier._timer = None
    return notifier


def test_dedup_set_grows_normally_below_cap(monkeypatch) -> None:
    """Below the soft cap the set keeps every key it has seen — no
    trimming, no behavior change for normal short-uptime cockpits."""
    notifier = _make_notifier(monkeypatch)

    fetched = [
        [_record(1), _record(2)],
        [_record(1), _record(2), _record(3)],
    ]
    calls = iter(fetched)

    with patch.object(notifier, "_fetch_alerts", lambda: next(calls)):
        notifier.poll_now()
        notifier.poll_now()

    assert {("id", 1), ("id", 2), ("id", 3)} == notifier._seen_alert_ids


def test_dedup_set_trims_to_currently_open_when_cap_exceeded(monkeypatch) -> None:
    """When the set crosses MAX_SEEN_ALERT_IDS, drop everything that
    isn't a currently-open alert. The closed-alert keys are gone but
    open-alert keys survive so we don't re-notify."""
    notifier = _make_notifier(monkeypatch)
    notifier.MAX_SEEN_ALERT_IDS = 5  # tighten the cap for the test

    # Pre-seed the set with 6 stale keys (above the cap).
    notifier._seen_alert_ids = {("id", i) for i in range(100, 106)}

    # Current open alerts: only ids 1 and 2.
    open_now = [_record(1), _record(2)]

    with patch.object(notifier, "_fetch_alerts", lambda: open_now):
        notifier.poll_now()

    # The 6 stale keys are gone; the 2 currently-open keys remain.
    assert notifier._seen_alert_ids == {("id", 1), ("id", 2)}


def test_dedup_set_trim_keeps_open_keys_to_avoid_renotify(monkeypatch) -> None:
    """After the trim, polling again with the same open alerts must
    NOT re-mount toasts — the open keys must survive the prune."""
    notifier = _make_notifier(monkeypatch)
    notifier.MAX_SEEN_ALERT_IDS = 3

    open_alerts = [_record(1, alert_type="test"), _record(2, alert_type="test")]

    # Seed past the cap.
    notifier._seen_alert_ids = {("id", i) for i in range(100, 110)}

    mount_count = {"n": 0}

    def fake_mount(_record):
        mount_count["n"] += 1
        return None  # don't actually create widgets

    with patch.object(notifier, "_fetch_alerts", lambda: open_alerts):
        with patch.object(notifier, "_mount_toast", fake_mount):
            notifier.poll_now()  # triggers trim; mounts 1+2 (new since seed had only stale ids)
            assert mount_count["n"] == 2
            notifier.poll_now()  # same open alerts → no re-mount
            assert mount_count["n"] == 2  # unchanged
