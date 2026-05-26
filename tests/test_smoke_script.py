from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke.py"
SPEC = importlib.util.spec_from_file_location("smoke_script", SCRIPT_PATH)
assert SPEC is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
assert SPEC.loader is not None
SPEC.loader.exec_module(smoke)


def test_task_command_construction() -> None:
    title = smoke.smoke_task_title(datetime(2026, 5, 23, 9, 8, 7))

    specs = smoke.task_command_specs("pollypm", title)

    assert title == "smoke-090807"
    # Description satisfies the queue-time has_description gate added by #2275.
    assert specs[0].argv == (
        "pm",
        "task",
        "create",
        "--project",
        "pollypm",
        "--description",
        (
            "Lane F smoke automation probe task — exercises pm task create/queue/get "
            "end-to-end. Safe to cancel/delete; no real work expected."
        ),
        "smoke-090807",
        "--json",
    )
    assert specs[1].argv == ("pm", "task", "queue", "{task_id}")
    assert specs[2].argv == ("pm", "task", "get", "{task_id}")


def test_fixed_command_construction_uses_public_cli() -> None:
    specs = smoke.fixed_command_specs()

    assert specs[0].argv == ("pm", "doctor")
    assert specs[0].fail_on_stdout == ("[FAIL]",)
    assert specs[1].name == "sessions health"
    assert specs[1].argv == ("pm", "sessions", "--health")


def test_summary_block_lists_failed_checks() -> None:
    results = [
        smoke.CheckResult("health", True, "ok"),
        smoke.CheckResult("dashboard", False, "HTTP 500"),
        smoke.CheckResult("doctor", False, "found [FAIL]"),
    ]

    summary = smoke.summary_block("abc123", "2026-05-23 10:00:00 MDT", results)

    assert "SHA: abc123" in summary
    assert "Result: red on dashboard, doctor" in summary
    assert "Failed checks: dashboard, doctor" in summary


def test_summary_block_all_green() -> None:
    summary = smoke.summary_block(
        "abc123",
        "2026-05-23 10:00:00 MDT",
        [smoke.CheckResult("health", True, "ok")],
    )

    assert "Result: all green" in summary
    assert "Failed checks: none" in summary


def test_run_smoke_exits_nonzero_when_slow_rest_check_fails(monkeypatch, capsys) -> None:
    def fake_rest_check(name: str, _url: str, **_kwargs) -> smoke.CheckResult:
        if name == "dashboard":
            return smoke.CheckResult(name, False, "slow response 1.234s", 1.234)
        return smoke.CheckResult(name, True, "HTTP 200 in 0.010s", 0.010)

    def fake_run_command(
        spec: smoke.CommandSpec,
        *,
        dry_run: bool = False,
    ) -> tuple[smoke.CheckResult, str]:
        stdout = '{"task_id": "pollypm/1"}' if spec.name == "task create" else ""
        return smoke.CheckResult(spec.name, True, "ok in 0.010s", 0.010), stdout

    monkeypatch.setattr(smoke, "run_rest_check", fake_rest_check)
    monkeypatch.setattr(smoke, "run_command", fake_run_command)
    monkeypatch.setattr(smoke, "read_token", lambda explicit_token=None: "token")
    monkeypatch.setattr(smoke, "git_sha", lambda: "abc123")
    args = SimpleNamespace(
        base="http://127.0.0.1:8897",
        token=None,
        project="pollypm",
        task_wait_seconds=0,
        dry_run=False,
        no_color=True,
    )

    exit_code = smoke.run_smoke(args)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "FAIL dashboard: slow response 1.234s" in output
    assert "Result: red on dashboard" in output
    assert "Failed checks: dashboard" in output


def test_dry_run_avoids_command_execution() -> None:
    result, stdout = smoke.run_command(
        smoke.CommandSpec("doctor", ("pm", "doctor")),
        dry_run=True,
    )

    assert result.ok
    assert result.skipped
    assert "DRY RUN pm doctor" == result.detail
    assert stdout == ""


def test_dry_run_rest_check_avoids_http() -> None:
    result = smoke.run_rest_check(
        "dashboard",
        "http://127.0.0.1:8765/api/v1/dashboard",
        token="secret",
        required_key="daemon_status",
        dry_run=True,
    )

    assert result.ok
    assert result.skipped
    assert "DRY RUN GET http://127.0.0.1:8765/api/v1/dashboard with bearer token" == result.detail


def test_extract_task_id_accepts_common_public_json_shapes() -> None:
    assert smoke.extract_task_id('{"task_id": "pollypm/1"}') == "pollypm/1"
    assert smoke.extract_task_id('{"project": "pollypm", "number": 2}') == "pollypm/2"
    assert smoke.extract_task_id("{}") is None
