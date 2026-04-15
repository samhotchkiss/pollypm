#!/usr/bin/env python3
"""Generate a markdown status report from the PollyPM task system."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


STATUS_ORDER = [
    "draft",
    "queued",
    "in_progress",
    "review",
    "done",
    "cancelled",
    "on_hold",
    "blocked",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "status-report.md"


def fetch_tasks(pm_command: str = "pm") -> list[dict]:
    """Read all tasks from the CLI as JSON."""
    result = subprocess.run(
        [pm_command, "task", "list", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise ValueError("Expected task list JSON output to be a list.")
    return data


def group_tasks(tasks: list[dict]) -> dict[str, list[dict]]:
    """Group tasks by work status while preserving the required status order."""
    grouped: dict[str, list[dict]] = {status: [] for status in STATUS_ORDER}
    extras: defaultdict[str, list[dict]] = defaultdict(list)

    for task in tasks:
        status = str(task.get("work_status", "draft"))
        if status in grouped:
            grouped[status].append(task)
        else:
            extras[status].append(task)

    for status in sorted(extras):
        grouped[status] = extras[status]
    return grouped


def _status_heading(status: str) -> str:
    return status.replace("_", " ").title()


def _task_line(task: dict) -> str:
    task_id = task.get("task_id", "-")
    title = task.get("title", "(untitled)")
    priority = task.get("priority", "-")
    assignee = task.get("assignee") or "-"
    current_node = task.get("current_node_id") or "-"
    return (
        f"- `{task_id}` | {title} | priority: `{priority}` | "
        f"assignee: `{assignee}` | node: `{current_node}`"
    )


def render_report(tasks: list[dict], generated_at: datetime | None = None) -> str:
    """Render the task list as markdown."""
    generated_at = generated_at or datetime.now(UTC)
    grouped = group_tasks(tasks)
    lines = [
        "# Project Status Report",
        "",
        f"Generated: {generated_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%SZ')}",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]

    for status in STATUS_ORDER:
        lines.append(f"| {_status_heading(status)} | {len(grouped.get(status, []))} |")

    extra_statuses = [status for status in grouped if status not in STATUS_ORDER]
    for status in extra_statuses:
        lines.append(f"| {_status_heading(status)} | {len(grouped[status])} |")

    for status in STATUS_ORDER + extra_statuses:
        tasks_in_status = grouped.get(status, [])
        lines.extend(
            [
                "",
                f"## {_status_heading(status)} ({len(tasks_in_status)})",
                "",
            ]
        )
        if tasks_in_status:
            lines.extend(_task_line(task) for task in tasks_in_status)
        else:
            lines.append("- None")

    lines.append("")
    return "\n".join(lines)


def write_report(content: str, output_path: Path = DEFAULT_OUTPUT) -> None:
    """Write the markdown report to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pm-command",
        default="pm",
        help="CLI command used to read tasks (default: pm)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Markdown output path (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks = fetch_tasks(pm_command=args.pm_command)
    report = render_report(tasks)
    write_report(report, output_path=args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
