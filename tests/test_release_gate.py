"""Tests for the release verification gate (#889)."""

from __future__ import annotations

from pollypm.release_gate import (
    DEFAULT_GATES,
    GateResult,
    GateSeverity,
    ReleaseReport,
    closure_comment_complete,
    gate_cockpit_interaction_audit_clean,
    gate_signal_routing_emitters_migrated,
    parse_closure_comment,
    run_release_gate,
)


# ---------------------------------------------------------------------------
# Issue-closure metadata parser (#889 acceptance criterion 4)
# ---------------------------------------------------------------------------


def test_parse_closure_comment_extracts_all_required_keys() -> None:
    """A complete closure comment exposes commit/branch/command/fresh."""
    text = (
        "Verified against:\n"
        "- commit: abc1234\n"
        "- branch: origin/main\n"
        "- command: pytest tests/test_foo.py\n"
        "- fresh restart: yes\n"
    )
    parsed = parse_closure_comment(text)
    assert parsed["commit"] == "abc1234"
    assert parsed["branch"] == "origin/main"
    assert parsed["command"] == "pytest tests/test_foo.py"
    assert parsed["fresh_restart"] == "yes"


def test_parse_closure_comment_rejects_invalid_hash() -> None:
    """A 'commit:' value that is not a plausible git hash is dropped.

    The audit cites the recurring shape: closures that name a
    branch but not a hash. A non-hash 'commit' string is treated
    as missing so the closure is reported incomplete."""
    parsed = parse_closure_comment("commit: WIP local changes\nbranch: main")
    assert "commit" not in parsed
    assert parsed["branch"] == "main"


def test_closure_comment_complete_flags_missing_keys() -> None:
    """Missing keys are returned in a stable order so the close
    UX can render an actionable checklist."""
    complete, missing = closure_comment_complete(
        "commit: abc1234\nbranch: origin/main"
    )
    assert complete is False
    assert "command" in missing
    assert "fresh_restart" in missing


def test_closure_comment_complete_passes_on_full_comment() -> None:
    text = (
        "commit: deadbeef\n"
        "branch: origin/main\n"
        "command: pytest -k cockpit\n"
        "fresh restart: yes\n"
    )
    complete, missing = closure_comment_complete(text)
    assert complete is True
    assert missing == ()


def test_closure_comment_tolerates_freeform_prose() -> None:
    """Real GitHub close comments mix free prose with key-value
    lines. The parser must extract what it can and ignore noise."""
    text = (
        "Closing — verified the rendered output includes the\n"
        "ctrl+q hint after a fresh cockpit restart.\n"
        "\n"
        "* Commit: abcdef0\n"
        "* Branch verified: origin/main\n"
        "* Commands run: pm up; pytest tests/test_keyboard_help.py\n"
        "* Cockpit restart: yes\n"
    )
    complete, missing = closure_comment_complete(text)
    assert complete is True, f"unexpected missing: {missing}"


# ---------------------------------------------------------------------------
# Built-in gates
# ---------------------------------------------------------------------------


def test_cockpit_interaction_audit_gate_passes_on_clean_registry() -> None:
    """With Tasks registered cleanly, the audit gate must pass."""
    result = gate_cockpit_interaction_audit_clean()
    assert result.passed is True, f"unexpected failure: {result.detail}"
    assert "registered" in result.summary.lower()


def test_signal_routing_emitters_gate_is_warning_when_unmigrated() -> None:
    """Until the high-traffic emitters migrate, the gate is a
    warning — not a blocking failure — so it does not mask the
    other gates' signals during the migration."""
    result = gate_signal_routing_emitters_migrated()
    # Migration is in progress; gate should be a warning.
    if not result.passed:
        assert result.severity is GateSeverity.WARNING
        assert "migrated" in result.summary or "missing" in result.summary


# ---------------------------------------------------------------------------
# run_release_gate aggregation
# ---------------------------------------------------------------------------


def test_run_release_gate_aggregates_default_gates() -> None:
    """The default gate run produces one result per gate."""
    report = run_release_gate()
    assert len(report.results) == len(DEFAULT_GATES)


def test_release_report_blocked_only_on_blocking_failure() -> None:
    """A warning-severity failure must not block the release."""
    report = ReleaseReport(
        results=[
            GateResult(
                name="warn_only",
                passed=False,
                severity=GateSeverity.WARNING,
                summary="not blocking",
            ),
            GateResult(name="ok", passed=True, summary="all good"),
        ]
    )
    assert report.blocked is False
    assert report.warnings != ()
    assert report.failures == ()


def test_release_report_blocked_on_blocking_failure() -> None:
    """A blocking-severity failure sets ``blocked``."""
    report = ReleaseReport(
        results=[
            GateResult(
                name="blocking_check",
                passed=False,
                severity=GateSeverity.BLOCKING,
                summary="this blocks",
            ),
        ]
    )
    assert report.blocked is True
    assert report.failures != ()


def test_run_release_gate_isolates_exceptions() -> None:
    """A gate that raises must not crash the gate runner — the
    failure becomes a synthetic failing result."""
    def explosive_gate() -> GateResult:
        raise RuntimeError("kaboom")

    report = run_release_gate(gates=[explosive_gate])
    assert len(report.results) == 1
    assert report.results[0].passed is False
    assert "kaboom" in (report.results[0].detail or "")


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_report_render_includes_verdict() -> None:
    """The first line of the rendered report names the verdict."""
    report = ReleaseReport(
        results=[GateResult(name="x", passed=True, summary="ok")]
    )
    assert report.render().splitlines()[0].startswith("Release gate:")


def test_report_render_marks_each_result() -> None:
    """Each gate's line carries a PASS/FAIL/WARN tag so a CI log
    reader can scan quickly."""
    report = ReleaseReport(
        results=[
            GateResult(name="a", passed=True, summary="ok"),
            GateResult(name="b", passed=False, summary="bad"),
            GateResult(
                name="c",
                passed=False,
                severity=GateSeverity.WARNING,
                summary="meh",
            ),
        ]
    )
    text = report.render()
    assert "[PASS] a" in text
    assert "[FAIL] b" in text
    assert "[WARN] c" in text
