"""Tests for the ``human_notify`` plugin.

Three layers of coverage:

1. **Protocol conformance** — each built-in adapter (macOS, webhook,
   cockpit) runtime-checks as ``HumanNotifyAdapter``.
2. **Dispatcher semantics** — HUMAN events are fanned out to every
   available adapter; non-HUMAN events are filtered; a failing
   adapter doesn't block its neighbors.
3. **Webhook config parsing** — the ``from_config`` helper is
   tolerant of garbage and reads the keys users actually write.

The macOS adapter's end-to-end delivery (actual ``osascript`` call)
is not covered here — that's an integration concern and shelling out
in CI can't be verified without a human watching Notification
Center. The webhook adapter's HTTP call is mocked with a fake
``urlopen`` so we can assert headers + body without a live server.
"""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from pollypm.plugins_builtin.human_notify.cockpit import CockpitNotifyAdapter
from pollypm.plugins_builtin.human_notify.dispatcher import (
    DispatchResult,
    dispatch,
    format_body,
    format_title,
)
from pollypm.plugins_builtin.human_notify.macos import MacOsNotifyAdapter, _escape
from pollypm.plugins_builtin.human_notify.protocol import HumanNotifyAdapter
from pollypm.plugins_builtin.human_notify.webhook import (
    WebhookNotifyAdapter,
    from_config,
)
from pollypm.work.models import ActorType


# ---------------------------------------------------------------------------
# Fake event — mimics the ``TaskAssignmentEvent`` shape
# ---------------------------------------------------------------------------


@dataclass
class _FakeEvent:
    task_id: str
    project: str
    actor_type: ActorType
    actor_name: str = "user"
    work_status: str = "review"
    task_title: str = "Plan ready for review: demo"
    execution_version: int = 0


def _human_event(task_id: str = "demo/1") -> _FakeEvent:
    return _FakeEvent(task_id=task_id, project="demo", actor_type=ActorType.HUMAN)


def _worker_event(task_id: str = "demo/2") -> _FakeEvent:
    return _FakeEvent(
        task_id=task_id, project="demo", actor_type=ActorType.ROLE,
        actor_name="worker",
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_macos_adapter_satisfies_protocol():
    assert isinstance(MacOsNotifyAdapter(), HumanNotifyAdapter)


def test_webhook_adapter_satisfies_protocol():
    assert isinstance(WebhookNotifyAdapter(url=None), HumanNotifyAdapter)


def test_cockpit_adapter_satisfies_protocol():
    assert isinstance(CockpitNotifyAdapter(store=None), HumanNotifyAdapter)


# ---------------------------------------------------------------------------
# is_available semantics
# ---------------------------------------------------------------------------


def test_macos_is_available_only_on_darwin(monkeypatch):
    # Force non-darwin — adapter short-circuits regardless of osascript.
    monkeypatch.setattr(sys, "platform", "linux")
    assert MacOsNotifyAdapter().is_available() is False


def test_webhook_is_available_requires_url():
    assert WebhookNotifyAdapter(url=None).is_available() is False
    assert WebhookNotifyAdapter(url="https://example.com").is_available() is True


def test_cockpit_is_available_requires_store():
    assert CockpitNotifyAdapter(store=None).is_available() is False

    class _Stub:
        def upsert_alert(self, *a, **kw): pass
    assert CockpitNotifyAdapter(store=_Stub()).is_available() is True


# ---------------------------------------------------------------------------
# Dispatcher semantics
# ---------------------------------------------------------------------------


class _Collecting:
    """Test adapter that records every notify() call it receives."""

    name = "collect"

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self._fail = fail
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return self._available

    def notify(self, *, title, body, task_id, project):
        if self._fail:
            raise RuntimeError("forced failure")
        self.calls.append({"title": title, "body": body, "task_id": task_id, "project": project})


def test_dispatch_human_event_delivers_to_available_adapters():
    a, b = _Collecting(), _Collecting()
    result = dispatch(_human_event(), [a, b])
    assert result == DispatchResult(delivered=2, skipped=0)
    assert a.calls[0]["task_id"] == "demo/1"
    assert b.calls[0]["project"] == "demo"


def test_dispatch_skips_unavailable_adapters():
    a = _Collecting(available=False)
    b = _Collecting()
    result = dispatch(_human_event(), [a, b])
    assert result == DispatchResult(delivered=1, skipped=1)
    assert a.calls == []
    assert len(b.calls) == 1


def test_dispatch_swallows_adapter_failure():
    bad = _Collecting(fail=True)
    good = _Collecting()
    result = dispatch(_human_event(), [bad, good])
    # Failed delivery is not counted, but good still delivered.
    assert result.delivered == 1
    assert len(good.calls) == 1


def test_dispatch_ignores_non_human_events():
    a = _Collecting()
    result = dispatch(_worker_event(), [a])
    assert result == DispatchResult(delivered=0, skipped=0)
    assert a.calls == []


def test_dispatch_title_and_body_are_short_and_actionable():
    event = _human_event()
    title = format_title(event)
    body = format_body(event)
    assert title.startswith("PollyPM:")
    assert "demo" in title
    assert event.task_id in body
    assert "pm inbox show" in body
    # Body should fit in a notification banner.
    assert len(body.splitlines()) <= 3


# ---------------------------------------------------------------------------
# Webhook: from_config tolerance + delivery
# ---------------------------------------------------------------------------


def test_webhook_from_config_empty_dict_disables():
    adapter = from_config({})
    assert adapter.is_available() is False


def test_webhook_from_config_reads_known_keys():
    adapter = from_config({
        "url": "https://ntfy.sh/test",
        "header_authorization": "Bearer abc",
        "title_header": "Title",
        "priority_header": "Priority",
        "default_priority": "5",
        "timeout_seconds": 2.5,
    })
    assert adapter.is_available() is True
    snapshot = adapter.as_dict()
    assert snapshot["url"] == "https://ntfy.sh/test"
    assert snapshot["authorization_set"] is True
    assert snapshot["default_priority"] == "5"
    assert snapshot["timeout_seconds"] == 2.5


def test_webhook_from_config_rejects_non_string_url():
    adapter = from_config({"url": 12345})  # garbage type
    assert adapter.is_available() is False


def test_webhook_from_config_fallback_timeout_on_bad_value():
    adapter = from_config({"url": "https://x", "timeout_seconds": "not-a-number"})
    # Falls back to the module default rather than crashing.
    assert adapter.timeout_seconds == 3.0


@contextmanager
def _patched_urlopen(*, status: int = 200):
    """Context manager that captures the Request passed to urlopen."""
    captured: dict = {}

    class _Response:
        def __init__(self, s): self.status = s
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data.decode("utf-8") if req.data else ""
        captured["timeout"] = timeout
        return _Response(status)

    with patch(
        "pollypm.plugins_builtin.human_notify.webhook.urllib.request.urlopen",
        new=_fake,
    ):
        yield captured


def test_webhook_notify_sends_expected_payload():
    adapter = WebhookNotifyAdapter(
        url="https://ntfy.sh/test",
        authorization="Bearer abc",
    )
    with _patched_urlopen() as captured:
        adapter.notify(
            title="PollyPM: Review — demo",
            body="Plan ready\npm inbox show demo/1",
            task_id="demo/1",
            project="demo",
        )
    assert captured["url"] == "https://ntfy.sh/test"
    assert captured["headers"]["Title"] == "PollyPM: Review — demo"
    assert captured["headers"]["X-pollypm-task"] == "demo/1"
    assert captured["headers"]["X-pollypm-project"] == "demo"
    assert captured["headers"]["Authorization"] == "Bearer abc"
    assert captured["data"].startswith("Plan ready")
    assert captured["timeout"] == 3.0


def test_webhook_notify_swallows_network_errors():
    import urllib.error as _uerr
    adapter = WebhookNotifyAdapter(url="https://example.invalid")

    def _raise(*_a, **_kw):
        raise _uerr.URLError("unreachable")

    with patch(
        "pollypm.plugins_builtin.human_notify.webhook.urllib.request.urlopen",
        new=_raise,
    ):
        # Must not raise — just logs + returns.
        adapter.notify(title="t", body="b", task_id="x/1", project="x")


# ---------------------------------------------------------------------------
# macOS adapter — escape + no-raise on subprocess failure
# ---------------------------------------------------------------------------


def test_macos_escape_escapes_quotes_and_backslashes():
    assert _escape('hello "world"') == 'hello \\"world\\"'
    assert _escape("path\\to\\thing") == "path\\\\to\\\\thing"


def test_macos_notify_swallows_subprocess_failure(monkeypatch):
    """A crashing osascript shouldn't raise into the dispatcher."""
    import subprocess

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(subprocess, "run", _raise)
    # Must not raise.
    MacOsNotifyAdapter().notify(
        title="t", body="b", task_id="x/1", project="x",
    )


# ---------------------------------------------------------------------------
# Cockpit adapter — writes an alert via upsert_alert
# ---------------------------------------------------------------------------


def test_cockpit_notify_upserts_alert_with_task_key():
    class _Store:
        def __init__(self): self.calls = []
        def upsert_alert(self, session, alert_type, severity, message):
            self.calls.append((session, alert_type, severity, message))

    store = _Store()
    CockpitNotifyAdapter(store=store).notify(
        title="PollyPM: Review — demo",
        body="Plan ready\npm inbox show demo/1",
        task_id="demo/1",
        project="demo",
    )
    assert len(store.calls) == 1
    session, alert_type, severity, message = store.calls[0]
    assert session == "task:demo/1"
    assert alert_type == "human_task_waiting"
    assert severity == "warn"
    assert "Plan ready" in message


def test_cockpit_notify_no_op_without_store():
    # Constructing with None must not raise; calling notify is a no-op.
    CockpitNotifyAdapter(store=None).notify(
        title="t", body="b", task_id="x/1", project="x",
    )
