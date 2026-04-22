from __future__ import annotations

import argparse

from demo_app import estimate_focus_minutes, summarize_queue
from demo_data import demo_task_titles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo-polly")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("summary", help="Print the queue summary.")
    subparsers.add_parser("tasks", help="Print the sample demo tasks.")
    subparsers.add_parser("focus", help="Print the focus estimate.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "summary":
        print(summarize_queue(demo_task_titles()))
        return 0
    if args.command == "tasks":
        for title in demo_task_titles():
            print(f"- {title}")
        return 0
    if args.command == "focus":
        print(f"{estimate_focus_minutes(len(demo_task_titles()))} minutes")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
