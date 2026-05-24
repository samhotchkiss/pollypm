#!/usr/bin/env python3
"""PollyPM HTTP performance harness.

This script intentionally stays outside ``src/pollypm`` and talks to PollyPM
only through the public Web API plus operator shell commands.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SAMPLES = 30
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_DURATION_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    request: RequestSpec
    mutates: bool = False


@dataclass(frozen=True)
class Sample:
    scenario: str
    status: int
    elapsed_seconds: float
    payload_bytes: int
    error: str | None = None


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile used by the §06 reference helper."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in the interval (0, 1]")
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def scenario_summary(samples: list[Sample]) -> dict[str, Any]:
    if not samples:
        raise ValueError("scenario_summary requires at least one sample")
    times = [sample.elapsed_seconds for sample in samples]
    sizes = [sample.payload_bytes for sample in samples]
    statuses = Counter(str(sample.status) for sample in samples)
    non_2xx = sum(
        count for status, count in statuses.items() if not status.startswith("2")
    )
    return {
        "samples": len(samples),
        "p50_seconds": percentile(times, 0.50),
        "p95_seconds": percentile(times, 0.95),
        "p99_seconds": percentile(times, 0.99),
        "max_seconds": max(times),
        "status_counts": dict(sorted(statuses.items())),
        "non_2xx_count": non_2xx,
        "payload_bytes": {
            "min": min(sizes),
            "median": percentile([float(size) for size in sizes], 0.50),
            "max": max(sizes),
            "mean": statistics.fmean(sizes),
        },
        "errors": [
            sample.error for sample in samples if sample.error is not None
        ],
    }


def default_scenarios(args: argparse.Namespace) -> dict[str, Scenario]:
    project = args.project
    task_number = args.task_number
    session = args.session
    inbox_id = args.inbox_id
    send_text = args.send_text
    return {
        "dashboard": Scenario(
            "dashboard",
            "GET /api/v1/dashboard",
            RequestSpec("GET", "/api/v1/dashboard"),
        ),
        "sessions": Scenario(
            "sessions",
            "GET /api/v1/chat/sessions",
            RequestSpec("GET", "/api/v1/chat/sessions"),
        ),
        "messages": Scenario(
            "messages",
            "GET recent messages for one chat surface",
            RequestSpec(
                "GET",
                f"/api/v1/chat/{quote_path(session)}/messages?limit=50&direction=desc",
            ),
        ),
        "task-list": Scenario(
            "task-list",
            "GET filtered task list",
            RequestSpec("GET", f"/api/v1/tasks?project={quote_query(project)}&limit=50"),
        ),
        "task-detail": Scenario(
            "task-detail",
            "GET one task detail",
            RequestSpec("GET", f"/api/v1/tasks/{quote_path(project)}/{task_number}"),
        ),
        "claim": Scenario(
            "claim",
            "POST task claim",
            RequestSpec(
                "POST",
                f"/api/v1/tasks/{quote_path(project)}/{task_number}/claim",
                {"actor": args.actor},
            ),
            mutates=True,
        ),
        "send": Scenario(
            "send",
            "POST chat send",
            RequestSpec(
                "POST",
                f"/api/v1/chat/{quote_path(session)}/send",
                {"text": send_text, "press_enter": True, "safety": args.send_safety},
            ),
            mutates=True,
        ),
        "inbox": Scenario(
            "inbox",
            "GET inbox list or one inbox item",
            RequestSpec(
                "GET",
                "/api/v1/inbox"
                if inbox_id is None
                else f"/api/v1/inbox/{quote_path(inbox_id)}",
            ),
        ),
    }


def quote_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def quote_query(value: str) -> str:
    return urllib.parse.quote_plus(value)


def parse_scenario_names(raw: str | None, scenarios: dict[str, Scenario]) -> list[str]:
    if raw is None or raw == "all":
        return list(scenarios)
    names = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name not in scenarios]
    if unknown:
        valid = ", ".join(scenarios)
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}; valid: {valid}")
    return names


def auth_token(args: argparse.Namespace) -> str | None:
    if args.token:
        return args.token
    env_token = os.environ.get("TOKEN") or os.environ.get("POLLYPM_API_TOKEN")
    if env_token:
        return env_token
    token_file = Path(args.token_file).expanduser()
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None


def normalized_base(base: str) -> str:
    value = base.strip().rstrip("/")
    if not value:
        raise SystemExit("base URL must not be empty")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"base URL must include http(s) scheme and host: {base!r}")
    return value


def build_url(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def perform_request(
    base: str,
    token: str | None,
    scenario: Scenario,
    *,
    timeout: float,
) -> Sample:
    url = build_url(base, scenario.request.path)
    headers = {"User-Agent": "pollypm-perf-harness/1"}
    data: bytes | None = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if scenario.request.body is not None:
        data = json.dumps(scenario.request.body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=scenario.request.method,
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status = int(response.status)
            error = None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = int(exc.code)
        error = f"HTTPError: {exc.reason}"
    except urllib.error.URLError as exc:
        payload = b""
        status = 0
        error = f"URLError: {exc.reason}"
    except TimeoutError as exc:
        payload = b""
        status = 0
        error = f"TimeoutError: {exc}"
    elapsed = time.perf_counter() - started
    return Sample(
        scenario=scenario.name,
        status=status,
        elapsed_seconds=elapsed,
        payload_bytes=len(payload),
        error=error,
    )


def run_measure(args: argparse.Namespace) -> dict[str, Any]:
    base = normalized_base(args.base)
    token = auth_token(args)
    scenarios = default_scenarios(args)
    selected_names = parse_scenario_names(args.scenarios, scenarios)
    selected = [scenarios[name] for name in selected_names]
    if args.require_token and token is None:
        raise SystemExit(
            "no API token found; set TOKEN/POLLYPM_API_TOKEN or pass --token/--token-file"
        )
    mutating = [scenario.name for scenario in selected if scenario.mutates]
    if mutating and not args.allow_mutating:
        raise SystemExit(
            "selected mutating scenario(s) require --allow-mutating: "
            + ", ".join(mutating)
        )
    if args.dry_run:
        return dry_run_report(args, base, token, selected)

    by_name: dict[str, list[Sample]] = {scenario.name: [] for scenario in selected}
    for scenario in selected:
        for _ in range(args.samples):
            sample = perform_request(base, token, scenario, timeout=args.timeout)
            by_name[scenario.name].append(sample)
            if args.verbose:
                print(
                    f"{scenario.name}\t{sample.status}\t"
                    f"{sample.elapsed_seconds:.6f}\t{sample.payload_bytes}",
                    flush=True,
                )
    return build_measure_report(args, base, selected, by_name)


def dry_run_report(
    args: argparse.Namespace,
    base: str,
    token: str | None,
    selected: list[Scenario],
) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "base": base,
        "samples": args.samples,
        "token_present": token is not None,
        "scenarios": [
            {
                "name": scenario.name,
                "method": scenario.request.method,
                "path": scenario.request.path,
                "mutates": scenario.mutates,
                "description": scenario.description,
            }
            for scenario in selected
        ],
    }


def build_measure_report(
    args: argparse.Namespace,
    base: str,
    selected: list[Scenario],
    by_name: dict[str, list[Sample]],
) -> dict[str, Any]:
    return {
        "mode": "measure",
        "generated_at": utc_now(),
        "base": base,
        "samples_per_scenario": args.samples,
        "timeout_seconds": args.timeout,
        "scenarios": {
            scenario.name: {
                "method": scenario.request.method,
                "path": scenario.request.path,
                "mutates": scenario.mutates,
                **scenario_summary(by_name[scenario.name]),
            }
            for scenario in selected
        },
        "raw_samples": [
            sample_to_json(sample)
            for scenario in selected
            for sample in by_name[scenario.name]
        ],
    }


def run_poll(args: argparse.Namespace) -> dict[str, Any]:
    base = normalized_base(args.base)
    token = auth_token(args)
    if args.require_token and token is None:
        raise SystemExit(
            "no API token found; set TOKEN/POLLYPM_API_TOKEN or pass --token/--token-file"
        )
    scenarios = {
        "dashboard": Scenario(
            "dashboard",
            "poll dashboard",
            RequestSpec("GET", "/api/v1/dashboard"),
        ),
        "sessions": Scenario(
            "sessions",
            "poll sessions",
            RequestSpec("GET", "/api/v1/chat/sessions"),
        ),
        "messages": Scenario(
            "messages",
            "poll recent operator messages",
            RequestSpec(
                "GET",
                f"/api/v1/chat/{quote_path(args.session)}/messages?limit=50&direction=desc",
            ),
        ),
    }
    selected_names = parse_scenario_names(args.scenarios, scenarios)
    selected = [scenarios[name] for name in selected_names]
    if args.dry_run:
        report = dry_run_report(args, base, token, selected)
        report["mode"] = "poll-dry-run"
        report["clients"] = args.clients
        report["duration_seconds"] = args.duration
        report["interval_seconds"] = args.interval
        return report

    deadline = time.monotonic() + args.duration
    samples: list[Sample] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.clients * len(selected)) as pool:
        futures = [
            pool.submit(
                poll_client,
                client_id,
                deadline,
                args.interval,
                base,
                token,
                selected,
                args.timeout,
            )
            for client_id in range(1, args.clients + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            samples.extend(future.result())
    by_name: dict[str, list[Sample]] = {scenario.name: [] for scenario in selected}
    for sample in samples:
        by_name[sample.scenario].append(sample)
    return {
        "mode": "poll",
        "generated_at": utc_now(),
        "base": base,
        "clients": args.clients,
        "duration_seconds": args.duration,
        "interval_seconds": args.interval,
        "scenarios": {
            scenario.name: scenario_summary(by_name[scenario.name])
            if by_name[scenario.name]
            else {"samples": 0}
            for scenario in selected
        },
        "raw_samples": [sample_to_json(sample) for sample in samples],
    }


def poll_client(
    client_id: int,
    deadline: float,
    interval: float,
    base: str,
    token: str | None,
    scenarios: list[Scenario],
    timeout: float,
) -> list[Sample]:
    del client_id
    samples: list[Sample] = []
    while time.monotonic() < deadline:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
            futures = [
                pool.submit(perform_request, base, token, scenario, timeout=timeout)
                for scenario in scenarios
            ]
            for future in concurrent.futures.as_completed(futures):
                samples.append(future.result())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return samples


def run_resources(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "resources",
        "generated_at": utc_now(),
        "processes": {
            "pm serve": process_snapshot(["pm", "serve"]),
            "pm cockpit": process_snapshot(["pm", "cockpit"]),
        },
        "postgres_connections": postgres_connections(args.database_url),
        "notes": [
            "fd counts use lsof when available",
            "postgres connections require psql and DATABASE_URL/--database-url",
        ],
    }


def process_snapshot(pattern: list[str]) -> list[dict[str, Any]]:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return []
    pattern_text = " ".join(pattern)
    result = subprocess.run(
        [pgrep, "-fl", pattern_text],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        pid = parts[0]
        command = parts[1] if len(parts) > 1 else ""
        rows.append(
            {
                "pid": int(pid),
                "command": command,
                "rss_kb": rss_kb(pid),
                "fd_count": fd_count(pid),
            }
        )
    return rows


def rss_kb(pid: str) -> int | None:
    ps = shutil.which("ps")
    if ps is None:
        return None
    result = subprocess.run(
        [ps, "-o", "rss=", "-p", pid],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def fd_count(pid: str) -> int | None:
    proc_fd = Path("/proc") / pid / "fd"
    if proc_fd.exists():
        try:
            return len(list(proc_fd.iterdir()))
        except OSError:
            return None
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    result = subprocess.run(
        [lsof, "-p", pid],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    return max(0, len(lines) - 1)


def postgres_connections(database_url: str | None) -> dict[str, Any]:
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        return {"available": False, "reason": "DATABASE_URL not set"}
    psql = shutil.which("psql")
    if psql is None:
        return {"available": False, "reason": "psql not found"}
    query = (
        "select state, count(*) "
        "from pg_stat_activity "
        "group by state "
        "order by state nulls first;"
    )
    result = subprocess.run(
        [psql, url, "-At", "-c", query],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return {
            "available": False,
            "reason": result.stderr.strip() or "psql failed",
        }
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        state, _, count = line.partition("|")
        counts[state or "null"] = int(count)
    return {"available": True, "state_counts": counts}


def sample_to_json(sample: Sample) -> dict[str, Any]:
    return {
        "scenario": sample.scenario,
        "status": sample.status,
        "elapsed_seconds": sample.elapsed_seconds,
        "payload_bytes": sample.payload_bytes,
        "error": sample.error,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_outputs(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.markdown_out:
        Path(args.markdown_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown_out).write_text(render_markdown(report), encoding="utf-8")
    if not args.quiet:
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_markdown(report))


def render_markdown(report: dict[str, Any]) -> str:
    mode = report.get("mode", "report")
    lines = [f"# PollyPM perf {mode}", ""]
    if "generated_at" in report:
        lines.append(f"- generated_at: `{report['generated_at']}`")
    if "base" in report:
        lines.append(f"- base: `{report['base']}`")
    if "samples_per_scenario" in report:
        lines.append(f"- samples_per_scenario: `{report['samples_per_scenario']}`")
    if "clients" in report:
        lines.append(f"- clients: `{report['clients']}`")
    if "duration_seconds" in report:
        lines.append(f"- duration_seconds: `{report['duration_seconds']}`")
    if "token_present" in report:
        lines.append(f"- token_present: `{report['token_present']}`")
    lines.append("")

    scenarios = report.get("scenarios")
    if isinstance(scenarios, dict):
        lines.extend(
            [
                "| Scenario | Samples | p50 | p95 | p99 | max | Status counts | Non-2xx | Max bytes |",
                "|---|---:|---:|---:|---:|---:|---|---:|---:|",
            ]
        )
        for name, summary in scenarios.items():
            if "p50_seconds" not in summary:
                lines.append(f"| {name} | {summary.get('samples', 0)} | | | | | | | |")
                continue
            payload = summary["payload_bytes"]
            lines.append(
                "| {name} | {samples} | {p50:.3f}s | {p95:.3f}s | "
                "{p99:.3f}s | {max_s:.3f}s | `{statuses}` | {non_2xx} | {max_b} |".format(
                    name=name,
                    samples=summary["samples"],
                    p50=summary["p50_seconds"],
                    p95=summary["p95_seconds"],
                    p99=summary["p99_seconds"],
                    max_s=summary["max_seconds"],
                    statuses=json.dumps(summary["status_counts"], sort_keys=True),
                    non_2xx=summary["non_2xx_count"],
                    max_b=payload["max"],
                )
            )
        lines.append("")
    elif isinstance(scenarios, list):
        lines.extend(["| Scenario | Method | Path | Mutates |", "|---|---|---|---:|"])
        for scenario in scenarios:
            lines.append(
                f"| {scenario['name']} | {scenario['method']} | "
                f"`{scenario['path']}` | {scenario['mutates']} |"
            )
        lines.append("")

    if mode == "resources":
        lines.extend(["## Process Snapshot", ""])
        for name, rows in report["processes"].items():
            lines.append(f"### {name}")
            if not rows:
                lines.extend(["No matching processes found.", ""])
                continue
            lines.extend(["| PID | RSS KB | FD count | Command |", "|---:|---:|---:|---|"])
            for row in rows:
                lines.append(
                    f"| {row['pid']} | {row['rss_kb']} | "
                    f"{row['fd_count']} | `{row['command']}` |"
                )
            lines.append("")
        lines.append("## Postgres Connections")
        lines.append("")
        lines.append(f"```json\n{json.dumps(report['postgres_connections'], indent=2)}\n```")
        lines.append("")

    return "\n".join(lines)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base",
        default=os.environ.get("BASE", "http://127.0.0.1:8765"),
        help="PollyPM Web API base URL (default: BASE env or http://127.0.0.1:8765)",
    )
    parser.add_argument("--token", help="Bearer token (default: TOKEN/POLLYPM_API_TOKEN env)")
    parser.add_argument(
        "--token-file",
        default="~/.pollypm/api-token",
        help="API token file used when env token is absent",
    )
    parser.add_argument(
        "--require-token",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="require a token before live HTTP requests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate config and list requests without hitting the daemon",
    )
    parser.add_argument("--json-out", help="write JSON report to this path")
    parser.add_argument("--markdown-out", help="write Markdown report to this path")
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="stdout format",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress stdout report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure PollyPM public Web API performance.",
    )
    subparsers = parser.add_subparsers(dest="command")

    measure = subparsers.add_parser(
        "measure",
        help="run fixed-sample endpoint measurements",
    )
    add_common_args(measure)
    measure.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help=f"samples per scenario (default: {DEFAULT_SAMPLES})",
    )
    measure.add_argument(
        "--scenarios",
        default="all",
        help=(
            "comma-separated scenarios or all "
            "(dashboard,sessions,messages,task-list,task-detail,claim,send,inbox)"
        ),
    )
    measure.add_argument("--project", default="pollypm", help="project for task scenarios")
    measure.add_argument("--task-number", type=int, default=1, help="task number")
    measure.add_argument("--session", default="operator", help="chat session name")
    measure.add_argument("--inbox-id", help="optional inbox item id for inbox detail")
    measure.add_argument("--actor", default="perf-harness", help="actor for claim")
    measure.add_argument(
        "--send-text",
        default="perf harness ping",
        help="message text for send scenario",
    )
    measure.add_argument(
        "--send-safety",
        choices=["strict", "loose", "force"],
        default="loose",
        help="chat send safety mode",
    )
    measure.add_argument(
        "--allow-mutating",
        action="store_true",
        help="allow claim/send scenarios to mutate daemon state",
    )
    measure.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    measure.add_argument("--verbose", action="store_true")
    measure.set_defaults(func=run_measure)

    poll = subparsers.add_parser(
        "poll",
        help="run browser-equivalent polling load from §6.5.1",
    )
    add_common_args(poll)
    poll.add_argument("--clients", type=int, default=10)
    poll.add_argument("--duration", type=float, default=DEFAULT_POLL_DURATION_SECONDS)
    poll.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    poll.add_argument("--session", default="operator")
    poll.add_argument(
        "--scenarios",
        default="all",
        help="comma-separated poll scenarios or all (dashboard,sessions,messages)",
    )
    poll.add_argument("--samples", type=int, default=0, help=argparse.SUPPRESS)
    poll.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    poll.set_defaults(func=run_poll)

    resources = subparsers.add_parser(
        "resources",
        help="snapshot pm serve/cockpit processes, fds, and PG connections",
    )
    resources.add_argument("--json-out", help="write JSON report to this path")
    resources.add_argument("--markdown-out", help="write Markdown report to this path")
    resources.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="stdout format",
    )
    resources.add_argument("--quiet", action="store_true")
    resources.add_argument("--database-url", help="Postgres URL; defaults to DATABASE_URL")
    resources.set_defaults(func=run_resources)

    scenarios = subparsers.add_parser("scenarios", help="list named scenarios")
    scenarios.add_argument("--project", default="pollypm")
    scenarios.add_argument("--task-number", type=int, default=1)
    scenarios.add_argument("--session", default="operator")
    scenarios.add_argument("--inbox-id")
    scenarios.add_argument("--actor", default="perf-harness")
    scenarios.add_argument("--send-text", default="perf harness ping")
    scenarios.add_argument("--send-safety", choices=["strict", "loose", "force"], default="loose")
    scenarios.add_argument("--json-out", help="write JSON report to this path")
    scenarios.add_argument("--markdown-out", help="write Markdown report to this path")
    scenarios.add_argument("--format", choices=["markdown", "json"], default="markdown")
    scenarios.add_argument("--quiet", action="store_true")
    scenarios.set_defaults(
        func=lambda args: {
            "mode": "scenarios",
            "scenarios": [
                {
                    "name": scenario.name,
                    "method": scenario.request.method,
                    "path": scenario.request.path,
                    "mutates": scenario.mutates,
                    "description": scenario.description,
                }
                for scenario in default_scenarios(args).values()
            ],
        }
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["measure", *(argv or [])])
    report = args.func(args)
    write_outputs(report, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
