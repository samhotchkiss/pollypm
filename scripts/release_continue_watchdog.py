#!/usr/bin/env python3
"""Watchdog for long-running PollyPM release burn-in work.

This does not keep an AI chat turn alive. It keeps the external work
observable and resumable:
- verifies the release-burnin tmux session exists;
- checks that the burn-in log is still advancing;
- writes a concise RESUME.md with current state and next actions.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from datetime import datetime, UTC
from pathlib import Path


DEFAULT_LOG_DIR = Path.home() / ".pollypm" / "release-burnin"
DEFAULT_REPO = Path("/Users/sam/dev/pollypm")


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run(args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _tmux_session_exists(name: str) -> bool:
    return _run(["tmux", "has-session", "-t", name], timeout=3).returncode == 0


def _monitor_log_age(log_dir: Path) -> int | None:
    monitor_log = log_dir / "monitor-pane.log"
    if not monitor_log.exists():
        return None
    return int(time.time() - monitor_log.stat().st_mtime)


def _tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _capture_pane(target: str, lines: int = 80) -> str:
    proc = _run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
    return proc.stdout if proc.returncode == 0 else proc.stderr


def _maybe_restart_burnin(
    repo: Path,
    log_dir: Path,
    session: str,
    *,
    stale_after: int,
) -> str | None:
    command = (
        f"cd {repo} && "
        "uv run python scripts/release_burnin.py --hours 12 --interval 180 "
        f"2>&1 | tee -a {log_dir / 'monitor-pane.log'}"
    )
    if _tmux_session_exists(session):
        age = _monitor_log_age(log_dir)
        if age is None or age <= stale_after:
            return None
        proc = _run(
            ["tmux", "respawn-pane", "-k", "-t", f"{session}:0.0", command],
            timeout=5,
        )
        if proc.returncode != 0:
            return f"failed to restart stale burn-in pane: {proc.stderr.strip()}"
        return f"restarted stale burn-in pane ({age}s since monitor update)"

    proc = _run(["tmux", "new-session", "-d", "-s", session, "-n", "monitor", command])
    if proc.returncode != 0:
        return f"failed to restart {session}: {proc.stderr.strip()}"
    return f"restarted missing tmux session {session}"


def _write_resume(
    *,
    log_dir: Path,
    repo: Path,
    burnin_session: str,
    stale_after: int,
    restart_note: str | None,
) -> None:
    monitor_log = log_dir / "monitor-pane.log"
    invariant_log = log_dir / "invariants-pane.log"
    tests_log = log_dir / "tests-pane.log"
    full_tests_log = log_dir / "full-tests.log"
    live_log = log_dir / "live-pane.log"
    resume_path = log_dir / "RESUME.md"

    now = time.time()
    if monitor_log.exists():
        age = int(now - monitor_log.stat().st_mtime)
        freshness = "fresh" if age <= stale_after else f"STALE ({age}s old)"
    else:
        age = -1
        freshness = "missing monitor log"

    latest_monitor = _tail(monitor_log, 60)
    latest_invariants = _tail(invariant_log, 40)
    latest_tests = _tail(tests_log, 40)
    latest_full_tests = _tail(full_tests_log, 40)
    latest_live = _tail(live_log, 40)
    burnin_capture = (
        _capture_pane(f"{burnin_session}:0.0", 80)
        if _tmux_session_exists(burnin_session)
        else "(release-burnin tmux session missing)"
    )

    blockers: list[str] = []
    combined = "\n".join(
        (latest_monitor, latest_invariants, latest_tests, latest_full_tests)
    )
    for line in combined.splitlines():
        if "FAIL " in line or "TASK_FLOW_FAIL" in line or "CAPTURE_FAIL" in line:
            blockers.append(line.strip())

    next_actions = [
        "Attach with `tmux attach -t release-burnin` and inspect pane 0 first.",
        "If pane 0 shows TASK_FLOW_FAIL/CAPTURE_FAIL, fix the root cause in code or the harness.",
        "If invariants show FAIL, reproduce with `uv run python scripts/release_invariants.py --config ~/.pollypm/pollypm.toml`.",
        "If focused tests fail, rerun the exact suite shown in pane 3 and patch the product code.",
        "Do not manually advance real project tasks; use product flows or fix the automation.",
    ]

    resume_path.write_text(
        "\n".join(
            [
                "# PollyPM Release Burn-in Resume",
                "",
                f"Updated: `{_ts()}`",
                f"Repo: `{repo}`",
                f"Burn-in tmux: `{burnin_session}`",
                f"Monitor freshness: `{freshness}`",
                f"Restart note: `{restart_note or 'none'}`",
                "",
                "## Current Blockers",
                *(f"- `{line}`" for line in blockers[:20]),
                *([] if blockers else ["- None detected in recent logs."]),
                "",
                "## Next Actions",
                *(f"- {action}" for action in next_actions),
                "",
                "## Latest Burn-in Pane",
                "```text",
                burnin_capture.strip(),
                "```",
                "",
                "## Latest Monitor Log",
                "```text",
                latest_monitor.strip(),
                "```",
                "",
                "## Latest Invariants",
                "```text",
                latest_invariants.strip(),
                "```",
                "",
                "## Latest Tests",
                "```text",
                latest_tests.strip(),
                "```",
                "",
                "## Latest Full Tests",
                "```text",
                latest_full_tests.strip(),
                "```",
                "",
                "## Latest Live Cockpit",
                "```text",
                latest_live.strip(),
                "```",
                "",
            ]
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--session", default="release-burnin")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--stale-after", type=int, default=600)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    while True:
        restart_note = None
        if args.restart:
            restart_note = _maybe_restart_burnin(
                args.repo,
                args.log_dir,
                args.session,
                stale_after=args.stale_after,
            )
        _write_resume(
            log_dir=args.log_dir,
            repo=args.repo,
            burnin_session=args.session,
            stale_after=args.stale_after,
            restart_note=restart_note,
        )
        print(f"[{_ts()}] wrote {args.log_dir / 'RESUME.md'}", flush=True)
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
