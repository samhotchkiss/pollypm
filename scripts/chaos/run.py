"""CLI entry point for PollyPM chaos injectors."""

from __future__ import annotations

from scripts.chaos.injectors import build_parser, dumps_result, run_from_args


def main() -> int:
    parser = build_parser()
    try:
        result = run_from_args(parser.parse_args())
    except ValueError as exc:
        result = {
            "passed": False,
            "error": str(exc),
            "safety": {
                "refused": True,
                "pg_touched": False,
                "tmux_touched": False,
                "real_account_touched": False,
            },
        }
        print(dumps_result(result))
        return 2
    print(dumps_result(result))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
