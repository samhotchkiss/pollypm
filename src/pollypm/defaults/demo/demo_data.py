"""Sample tasks for the offline PollyPM demo."""

from __future__ import annotations

DEMO_TASKS: list[dict[str, str]] = [
    {"title": "Fix the queue estimate bug", "kind": "bug"},
    {"title": "Write a demo launch note", "kind": "docs"},
    {"title": "Review the seeded git history", "kind": "ops"},
]


def demo_task_titles() -> list[str]:
    return [item["title"] for item in DEMO_TASKS]
