#!/usr/bin/env python3
"""Lane F smoke automation for PollyPM.

This harness intentionally stays outside ``src/pollypm`` and talks to PollyPM
only through public REST endpoints and the ``pm`` CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 30.0
    fail_on_stdout: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    elapsed_seconds: float = 0.0
    skipped: bool = False


def read_token(explicit_token: str | None = None) -> str | None:
    if explicit_token:
        return explicit_token
    if token := os.environ.get("TOKEN"):
        return token
    token_file = Path.home() / ".pollypm" / "api-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None


def git_sha() -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def smoke_task_title(now: datetime) -> str:
    return f"smoke-{now.strftime('%H%M%S')}"


def task_command_specs(project: str, title: str) -> list[CommandSpec]:
    # Description satisfies the queue-time has_description gate added by #2275.
    smoke_description = (
        "Lane F smoke automation probe task — exercises pm task create/queue/get end-to-end. "
        "Safe to cancel/delete; no real work expected."
    )
    return [
        CommandSpec(
            name="task create",
            argv=(
                "pm", "task", "create",
                "--project", project,
                "--description", smoke_description,
                title,
                "--json",
            ),
            timeout_seconds=30,
        ),
        CommandSpec(
            name="task queue",
            argv=("pm", "task", "queue", "{task_id}"),
            timeout_seconds=30,
        ),
        CommandSpec(
            name="task get",
            argv=("pm", "task", "get", "{task_id}"),
            timeout_seconds=30,
        ),
    ]


def fixed_command_specs() -> list[CommandSpec]:
    return [
        CommandSpec(
            name="doctor",
            argv=("pm", "doctor"),
            timeout_seconds=120,
            fail_on_stdout=("[FAIL]",),
        ),
        CommandSpec(
            name="sessions health",
            argv=("pm", "sessions", "--health"),
            timeout_seconds=60,
        ),
    ]


def extract_task_id(stdout: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    for key in ("task_id", "id", "ref"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    project = payload.get("project")
    number = payload.get("number")
    if isinstance(project, str) and isinstance(number, int):
        return f"{project}/{number}"
    return None


def run_command(spec: CommandSpec, *, dry_run: bool = False) -> tuple[CheckResult, str]:
    rendered = " ".join(spec.argv)
    if dry_run:
        return CheckResult(spec.name, True, f"DRY RUN {rendered}", skipped=True), ""

    started = time.monotonic()
    try:
        proc = subprocess.run(
            spec.argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        return CheckResult(spec.name, False, f"timed out after {spec.timeout_seconds:g}s", elapsed), ""

    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        detail = f"exit {proc.returncode}: {output.strip()[:300]}"
        return CheckResult(spec.name, False, detail, elapsed), proc.stdout
    for marker in spec.fail_on_stdout:
        if marker.lower() in output.lower():
            return CheckResult(
                spec.name,
                False,
                f"found disallowed marker {marker!r}",
                elapsed,
            ), proc.stdout
    return CheckResult(spec.name, True, f"ok in {elapsed:.3f}s", elapsed), proc.stdout


def get_json(url: str, *, token: str | None, timeout_seconds: float = 5.0) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


# Endpoints whose timing is asserted against the §07 sub-second contract.
# These get a discarded warmup hit before the measured request so a cold-cache
# first-hit (e.g. 1.2s on a fresh `pm serve`) does not red the smoke when the
# steady-state response is well under budget (§06.4 dashboard warm p95 < 150ms).
WARMUP_REST_CHECKS: frozenset[str] = frozenset({"dashboard", "sessions"})
WARM_RESPONSE_BUDGET_SECONDS: float = 1.0


def run_rest_check(
    name: str,
    url: str,
    *,
    token: str | None,
    required_key: str | None = None,
    expected_value: tuple[str, Any] | None = None,
    require_nonempty_sessions: bool = False,
    dry_run: bool = False,
) -> CheckResult:
    if dry_run:
        auth = " with bearer token" if token else ""
        return CheckResult(name, True, f"DRY RUN GET {url}{auth}", skipped=True)

    # Discard a warmup hit so the measured request reflects steady-state
    # response time, not cold-start cache population. See §07 "comfortably
    # under 1s" + §06.4 warm budgets.
    if name in WARMUP_REST_CHECKS:
        try:
            get_json(url, token=token)
        except Exception:
            # Surface the real error on the measured attempt below; if the
            # warmup hit is genuinely broken the measured call will fail too.
            pass

    started = time.monotonic()
    try:
        status, payload = get_json(url, token=token)
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        return CheckResult(name, False, f"HTTP {exc.code}", elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - started
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}", elapsed)

    elapsed = time.monotonic() - started
    if status != 200:
        return CheckResult(name, False, f"HTTP {status}", elapsed)
    if required_key and required_key not in payload:
        return CheckResult(name, False, f"missing JSON key {required_key!r}", elapsed)
    if expected_value:
        key, expected = expected_value
        if payload.get(key) != expected:
            return CheckResult(
                name,
                False,
                f"expected JSON {key}={expected!r}",
                elapsed,
            )
    if require_nonempty_sessions:
        sessions = payload.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return CheckResult(name, False, "expected non-empty sessions list", elapsed)
    if elapsed > WARM_RESPONSE_BUDGET_SECONDS and name in WARMUP_REST_CHECKS:
        return CheckResult(name, False, f"slow response {elapsed:.3f}s", elapsed)
    return CheckResult(name, True, f"HTTP 200 in {elapsed:.3f}s", elapsed)


def print_result(result: CheckResult, *, color: bool = True) -> None:
    if result.ok:
        label = "SKIP" if result.skipped else "PASS"
        hue = YELLOW if result.skipped else GREEN
    else:
        label = "FAIL"
        hue = RED
    prefix = f"{hue}{label}{RESET}" if color else label
    print(f"{prefix} {result.name}: {result.detail}")


def failed_check_names(results: list[CheckResult]) -> list[str]:
    return [result.name for result in results if not result.ok]


def summary_block(sha: str, timestamp: str, results: list[CheckResult]) -> str:
    failed = failed_check_names(results)
    result_text = "all green" if not failed else f"red on {', '.join(failed)}"
    failed_text = "none" if not failed else ", ".join(failed)
    return "\n".join(
        [
            "Smoke summary:",
            f"Smoke pass: {timestamp}",
            f"SHA: {sha}",
            f"Result: {result_text}",
            f"Failed checks: {failed_text}",
        ]
    )


def run_smoke(args: argparse.Namespace) -> int:
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sha = git_sha()
    token = read_token(args.token)
    base = args.base.rstrip("/")
    results: list[CheckResult] = []

    rest_checks = [
        ("health", f"{base}/api/v1/health", None, ("status", "ok"), False, None),
        ("dashboard", f"{base}/api/v1/dashboard", "daemon_status", None, False, token),
        ("sessions", f"{base}/api/v1/chat/sessions", None, None, True, token),
    ]
    for name, url, required_key, expected_value, require_sessions, auth_token in rest_checks:
        result = run_rest_check(
            name,
            url,
            token=auth_token,
            required_key=required_key,
            expected_value=expected_value,
            require_nonempty_sessions=require_sessions,
            dry_run=args.dry_run,
        )
        results.append(result)
        print_result(result, color=not args.no_color)

    now = datetime.now()
    task_id = "{task_id}"
    for spec in task_command_specs(args.project, smoke_task_title(now)):
        argv = tuple(part.format(task_id=task_id) for part in spec.argv)
        result, stdout = run_command(
            CommandSpec(spec.name, argv, spec.timeout_seconds, spec.fail_on_stdout),
            dry_run=args.dry_run,
        )
        if spec.name == "task create" and result.ok and not args.dry_run:
            parsed_task_id = extract_task_id(stdout)
            if not parsed_task_id:
                result = CheckResult("task create", False, "could not parse task id from JSON")
            else:
                task_id = parsed_task_id
        results.append(result)
        print_result(result, color=not args.no_color)
        if not result.ok:
            break
        if spec.name == "task queue" and args.task_wait_seconds > 0 and not args.dry_run:
            time.sleep(args.task_wait_seconds)

    if not failed_check_names(results):
        for spec in fixed_command_specs():
            result, _stdout = run_command(spec, dry_run=args.dry_run)
            results.append(result)
            print_result(result, color=not args.no_color)

    print()
    if args.no_color:
        print(summary_block(sha, timestamp, results))
    else:
        block = summary_block(sha, timestamp, results)
        color = GREEN if not failed_check_names(results) else RED
        print(f"{BOLD}{color}{block}{RESET}")
    return 1 if failed_check_names(results) else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PollyPM lane F smoke checks.")
    parser.add_argument("--base", default=os.environ.get("BASE", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=None, help="API bearer token; defaults to TOKEN or ~/.pollypm/api-token")
    parser.add_argument("--project", default="pollypm", help="project for task create/queue/get")
    parser.add_argument("--task-wait-seconds", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true", help="print planned checks without hitting daemon or CLI")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run_smoke(parse_args(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
