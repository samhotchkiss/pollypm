"""Tests for the self-bug-report → GitHub helper (#1569).

Covers :mod:`pollypm.audit.bug_reporter` — the leaf module that
routes Polly/heartbeat self-bug-report observations to a GitHub
issue with the ``polly-self-report`` label instead of dropping them
into the user's inbox.

Test surface:

* Happy path — ``gh issue create`` runs with the right argv and the
  helper returns the parsed issue number.
* Dedup — filing the same title twice within the window only invokes
  ``gh issue create`` once.
* Failure modes — no ``gh`` on PATH / non-zero exit / malformed
  output all return ``None`` and emit an audit event so forensic
  reads can correlate.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from pollypm.audit import bug_reporter


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _isoformat_now_minus(seconds: int) -> str:
    from datetime import timedelta

    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _redirect_audit_home(tmp_path, monkeypatch):
    """Keep audit writes out of the user's real ``~/.pollypm/audit``."""
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit"))
    monkeypatch.delenv(bug_reporter._DISABLE_ENV, raising=False)
    monkeypatch.setenv(bug_reporter._REPO_ENV, "owner/repo")


@pytest.fixture
def _gh_on_path(monkeypatch):
    """Pretend ``gh`` is installed."""
    monkeypatch.setattr(
        bug_reporter, "_gh_available", lambda: True,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_creates_issue_and_returns_number(_gh_on_path):
    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "issue", "list"]:
            # No existing issues to dedup against.
            return _completed(stdout="[]\n")
        if cmd[:3] == ["gh", "issue", "create"]:
            return _completed(
                stdout="https://github.com/owner/repo/issues/4242\n",
            )
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    with patch("subprocess.run", side_effect=fake_run):
        number = bug_reporter.file_bug_report(
            title="Heartbeat respawn loop on tier-3 dispatch",
            body="Polly noticed the watchdog re-spawned three times in 90s.",
            actor="heartbeat",
            project="savethenovel",
            subject="watchdog-tier3",
        )
    assert number == 4242

    create_call = next(
        c for c in calls if c[:3] == ["gh", "issue", "create"]
    )
    # Title + body + label make it onto the argv.
    assert "--title" in create_call
    assert (
        create_call[create_call.index("--title") + 1]
        == "Heartbeat respawn loop on tier-3 dispatch"
    )
    assert "--label" in create_call
    assert (
        bug_reporter.SELF_REPORT_LABEL
        in [create_call[i + 1] for i, v in enumerate(create_call) if v == "--label"]
    )
    # Footer was appended to the body.
    body_arg = create_call[create_call.index("--body") + 1]
    assert "actor: heartbeat" in body_arg
    assert "project: savethenovel" in body_arg
    assert "subject: watchdog-tier3" in body_arg


def test_detailed_result_marks_created_true(_gh_on_path):
    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            return _completed(stdout="[]\n")
        return _completed(stdout="https://github.com/owner/repo/issues/7\n")

    with patch("subprocess.run", side_effect=fake_run):
        result = bug_reporter.file_bug_report_detailed(
            title="Inbox sweep ran twice in one tick",
            body="evidence dump",
        )
    assert result is not None
    assert result.created is True
    assert result.issue_number == 7
    assert result.title == "Inbox sweep ran twice in one tick"


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_dedup_within_window_skips_create(_gh_on_path):
    """A matching open issue inside the window short-circuits create."""
    create_calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            payload = [
                {
                    "number": 1234,
                    "title": "Heartbeat respawn loop",
                    "createdAt": _isoformat_now_minus(5 * 60),
                },
            ]
            return _completed(stdout=json.dumps(payload))
        if cmd[:3] == ["gh", "issue", "create"]:
            create_calls.append(list(cmd))
            return _completed(stdout="https://github.com/owner/repo/issues/9999\n")
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    with patch("subprocess.run", side_effect=fake_run):
        result = bug_reporter.file_bug_report_detailed(
            title="Heartbeat respawn loop",
            body="re-observation",
        )
    assert result is not None
    assert result.created is False
    assert result.issue_number == 1234
    assert create_calls == []


def test_dedup_skips_match_outside_window(_gh_on_path):
    """A match older than the window does NOT suppress a fresh create."""

    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            payload = [
                {
                    "number": 11,
                    "title": "Heartbeat respawn loop",
                    # 2 hours ago — outside the default 1h window.
                    "createdAt": _isoformat_now_minus(2 * 60 * 60),
                },
            ]
            return _completed(stdout=json.dumps(payload))
        return _completed(stdout="https://github.com/owner/repo/issues/55\n")

    with patch("subprocess.run", side_effect=fake_run):
        number = bug_reporter.file_bug_report(
            title="Heartbeat respawn loop",
            body="fresh observation",
        )
    assert number == 55


def test_dedup_ignores_partial_title_matches(_gh_on_path):
    """``gh issue list`` ``in:title`` returns substring matches — the helper
    must only dedup on EXACT title equality, not substring.
    """

    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            payload = [
                {
                    "number": 9,
                    "title": "Heartbeat respawn loop on tier-3 (followup)",
                    "createdAt": _isoformat_now_minus(60),
                },
            ]
            return _completed(stdout=json.dumps(payload))
        return _completed(stdout="https://github.com/owner/repo/issues/77\n")

    with patch("subprocess.run", side_effect=fake_run):
        number = bug_reporter.file_bug_report(
            title="Heartbeat respawn loop",
            body="x",
        )
    assert number == 77  # Not 9 — title differs.


def test_dedup_window_zero_always_creates(_gh_on_path):
    """``dedup_window_seconds=0`` disables dedup — every call creates."""
    create_calls = 0

    def fake_run(cmd, *_a, **_kw):
        nonlocal create_calls
        if cmd[:3] == ["gh", "issue", "list"]:
            # Should NOT be reached when window=0.
            raise AssertionError("dedup query fired with window=0")
        if cmd[:3] == ["gh", "issue", "create"]:
            create_calls += 1
            return _completed(stdout=f"https://github.com/owner/repo/issues/{create_calls}\n")
        raise AssertionError(f"unexpected: {cmd!r}")

    with patch("subprocess.run", side_effect=fake_run):
        n1 = bug_reporter.file_bug_report(
            title="Same title", body="x", dedup_window_seconds=0,
        )
        n2 = bug_reporter.file_bug_report(
            title="Same title", body="x", dedup_window_seconds=0,
        )
    assert n1 == 1
    assert n2 == 2


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_gh_missing_returns_none_and_emits_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(bug_reporter, "_gh_available", lambda: False)

    with patch("subprocess.run") as run_mock:
        number = bug_reporter.file_bug_report(
            title="Bug observation",
            body="x",
            project="demo",
        )
    assert number is None
    # Helper never shelled out.
    run_mock.assert_not_called()

    # Audit event was recorded with the error class.
    from pollypm.audit.log import read_events

    events = read_events("demo")
    failures = [e for e in events if e.event == bug_reporter.EVENT_BUG_REPORT_FAILED]
    assert failures, f"expected EVENT_BUG_REPORT_FAILED, got: {[e.event for e in events]}"
    assert failures[-1].metadata.get("error") == "gh_unavailable"


def test_gh_create_non_zero_returns_none(_gh_on_path):
    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            return _completed(stdout="[]\n")
        return _completed(
            stdout="",
            returncode=1,
            stderr="GraphQL: label not found",
        )

    with patch("subprocess.run", side_effect=fake_run):
        number = bug_reporter.file_bug_report(
            title="x", body="x", project="demo",
        )
    assert number is None

    from pollypm.audit.log import read_events

    events = read_events("demo")
    failures = [
        e for e in events if e.event == bug_reporter.EVENT_BUG_REPORT_FAILED
    ]
    assert failures[-1].metadata.get("error") == "gh_create_failed"


def test_empty_title_refused(_gh_on_path):
    with patch("subprocess.run") as run_mock:
        number = bug_reporter.file_bug_report(title="   ", body="x")
    assert number is None
    run_mock.assert_not_called()


def test_disabled_env_short_circuits(monkeypatch, _gh_on_path):
    monkeypatch.setenv(bug_reporter._DISABLE_ENV, "1")
    with patch("subprocess.run") as run_mock:
        number = bug_reporter.file_bug_report(
            title="x", body="x", project="demo",
        )
    assert number is None
    run_mock.assert_not_called()

    from pollypm.audit.log import read_events

    events = read_events("demo")
    failures = [
        e for e in events if e.event == bug_reporter.EVENT_BUG_REPORT_FAILED
    ]
    assert failures[-1].metadata.get("error") == "disabled_by_env"


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_issue_number_from_url_with_trailing_whitespace():
    assert (
        bug_reporter._parse_issue_number(
            "https://github.com/owner/repo/issues/1234\n"
        )
        == 1234
    )


def test_parse_issue_number_returns_none_on_garbage():
    assert bug_reporter._parse_issue_number("not a url") is None
    assert bug_reporter._parse_issue_number("") is None


def test_parse_issue_number_handles_query_string():
    assert (
        bug_reporter._parse_issue_number(
            "https://github.com/o/r/issues/77?utm_source=cli"
        )
        == 77
    )


# ---------------------------------------------------------------------------
# Audit event on success
# ---------------------------------------------------------------------------


def test_filed_event_carries_issue_number(_gh_on_path):
    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            return _completed(stdout="[]\n")
        return _completed(stdout="https://github.com/owner/repo/issues/321\n")

    with patch("subprocess.run", side_effect=fake_run):
        bug_reporter.file_bug_report(
            title="audit-trail event check",
            body="x",
            project="demo",
            subject="sub-1",
        )

    from pollypm.audit.log import read_events

    events = read_events("demo")
    filed = [
        e for e in events if e.event == bug_reporter.EVENT_BUG_REPORT_FILED
    ]
    assert filed
    assert filed[-1].metadata.get("issue_number") == 321
    assert filed[-1].metadata.get("title") == "audit-trail event check"
    assert filed[-1].subject == "sub-1"


def test_deduped_event_carries_issue_number(_gh_on_path):
    def fake_run(cmd, *_a, **_kw):
        if cmd[:3] == ["gh", "issue", "list"]:
            payload = [
                {
                    "number": 88,
                    "title": "dup-title",
                    "createdAt": _isoformat_now_minus(60),
                },
            ]
            return _completed(stdout=json.dumps(payload))
        raise AssertionError("create should not run for dedup match")

    with patch("subprocess.run", side_effect=fake_run):
        number = bug_reporter.file_bug_report(
            title="dup-title", body="x", project="demo",
        )
    assert number == 88

    from pollypm.audit.log import read_events

    events = read_events("demo")
    dedup = [
        e for e in events if e.event == bug_reporter.EVENT_BUG_REPORT_DEDUPED
    ]
    assert dedup
    assert dedup[-1].metadata.get("issue_number") == 88
