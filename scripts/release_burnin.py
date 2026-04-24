#!/usr/bin/env python3
"""12-hour release burn-in loop for PollyPM.

The loop intentionally combines:
- real tmux cockpit captures from the live session;
- invariant checks against configured project DBs;
- a disposable task-flow lab driven through ``pm task`` CLI commands.

It keeps running until interrupted or until ``--hours`` elapses.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime, UTC
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / ".pollypm" / "pollypm.toml"
LAB_ROOT = Path("/tmp/pollypm-release-burnin")
LAB_PROJECT = "burnin"
LAB_DB = LAB_ROOT / "project" / ".pollypm" / "state.db"


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run(
    args: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        cmd = " ".join(shlex.quote(arg) for arg in args)
        raise RuntimeError(
            f"{cmd} failed with {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def _pm(*args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return _run(["pm", *args], check=check, timeout=timeout)


def _task_id_from_json(stdout: str) -> str:
    payload = json.loads(stdout)
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or "/" not in task_id:
        raise RuntimeError(f"could not parse task_id from {stdout!r}")
    return task_id


def _ensure_lab() -> None:
    project = LAB_ROOT / "project"
    project.mkdir(parents=True, exist_ok=True)
    LAB_DB.parent.mkdir(parents=True, exist_ok=True)


def _task_create(title: str, *extra: str) -> str:
    proc = _pm(
        "task",
        "create",
        title,
        "--project",
        LAB_PROJECT,
        "--description",
        f"Release burn-in task created at {_ts()}.",
        "--role",
        "worker=burnin-worker",
        "--role",
        "reviewer=burnin-reviewer",
        "--db",
        str(LAB_DB),
        "--json",
        *extra,
    )
    return _task_id_from_json(proc.stdout)


def _task_status(task_id: str) -> str:
    proc = _pm("task", "get", task_id, "--db", str(LAB_DB), "--json")
    payload = json.loads(proc.stdout)
    return str(payload.get("work_status") or "")


def run_task_flow_lab() -> list[str]:
    """Run one disposable CLI-only task-flow pass."""
    if LAB_ROOT.exists():
        shutil.rmtree(LAB_ROOT)
    _ensure_lab()
    lines: list[str] = []

    normal = _task_create(
        f"burn-in normal {_ts()}",
        "--role",
        "worker=burnin-worker",
        "--role",
        "reviewer=burnin-reviewer",
        "--acceptance-criteria",
        "CLI flow reaches review and final approval.",
    )
    lines.append(f"normal={normal}")
    _pm("task", "queue", normal, "--db", str(LAB_DB))
    _pm("task", "claim", normal, "--actor", "burnin-worker", "--db", str(LAB_DB))
    _pm(
        "task",
        "done",
        normal,
        "--actor",
        "burnin-worker",
        "--db",
        str(LAB_DB),
        "--output",
        json.dumps(
            {
                "type": "code_change",
                "summary": "burn-in pass",
                "artifacts": [
                    {"kind": "note", "description": "CLI burn-in artifact"}
                ],
            }
        ),
    )
    _pm(
        "task",
        "approve",
        normal,
        "--actor",
        "burnin-reviewer",
        "--reason",
        "burn-in approval",
        "--db",
        str(LAB_DB),
    )
    lines.append(f"{normal} status={_task_status(normal)}")

    rejected = _task_create(
        f"burn-in rejection {_ts()}",
        "--role",
        "worker=burnin-worker",
        "--role",
        "reviewer=burnin-reviewer",
        "--acceptance-criteria",
        "Reject returns work to an advanceable state.",
    )
    _pm("task", "queue", rejected, "--db", str(LAB_DB))
    _pm("task", "claim", rejected, "--actor", "burnin-worker", "--db", str(LAB_DB))
    _pm(
        "task",
        "done",
        rejected,
        "--actor",
        "burnin-worker",
        "--db",
        str(LAB_DB),
        "--output",
        json.dumps(
            {
                "type": "code_change",
                "summary": "needs rejection",
                "artifacts": [
                    {"kind": "note", "description": "CLI burn-in rejection artifact"}
                ],
            }
        ),
    )
    _pm(
        "task",
        "reject",
        rejected,
        "--actor",
        "burnin-reviewer",
        "--reason",
        "burn-in rejection",
        "--db",
        str(LAB_DB),
    )
    lines.append(f"{rejected} status_after_reject={_task_status(rejected)}")

    human = _task_create(
        f"burn-in human review {_ts()}",
        "--requires-human-review",
        "--role",
        "worker=burnin-worker",
        "--role",
        "reviewer=burnin-reviewer",
    )
    blocked_queue = _pm(
        "task",
        "queue",
        human,
        "--db",
        str(LAB_DB),
        check=False,
    )
    lines.append(f"{human} initial_queue_rc={blocked_queue.returncode}")
    _pm(
        "task",
        "approve-human-review",
        human,
        "--fast-track-authorized",
        "--reason",
        "burn-in authorization",
        "--db",
        str(LAB_DB),
    )
    _pm("task", "queue", human, "--db", str(LAB_DB))
    lines.append(f"{human} status_after_human_review={_task_status(human)}")

    blocker = _task_create(f"burn-in blocker {_ts()}")
    blocked = _task_create(f"burn-in blocked {_ts()}")
    _pm("task", "queue", blocked, "--db", str(LAB_DB))
    _pm("task", "claim", blocked, "--actor", "burnin-worker", "--db", str(LAB_DB))
    _pm("task", "block", blocked, "--blocker", blocker, "--db", str(LAB_DB))
    lines.append(f"{blocked} status_after_block={_task_status(blocked)}")
    return lines


def capture_live_cockpit(config: Path, out_dir: Path) -> list[str]:
    from pollypm.config import load_config

    config_obj = load_config(config)
    session = config_obj.project.tmux_session
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    left = _run(
        ["tmux", "capture-pane", "-t", f"{session}:0.0", "-p", "-S", "-100"],
        check=False,
        timeout=5,
    )
    right = _run(
        ["tmux", "capture-pane", "-t", f"{session}:0.1", "-p", "-S", "-140"],
        check=False,
        timeout=5,
    )
    (out_dir / f"{stamp}-left.txt").write_text(left.stdout)
    (out_dir / f"{stamp}-right.txt").write_text(right.stdout)
    return [
        f"captured live cockpit rc=({left.returncode},{right.returncode})",
        f"right_contains_action_needed={'Action Needed' in right.stdout}",
    ]


def run_invariants(config: Path) -> tuple[int, str]:
    proc = _run(
        [sys.executable, str(ROOT / "scripts" / "release_invariants.py"), "--config", str(config)],
        check=False,
        timeout=30,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--interval", type=int, default=180)
    parser.add_argument("--log-dir", type=Path, default=Path.home() / ".pollypm" / "release-burnin")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / "burnin.log"
    captures = args.log_dir / "captures"
    deadline = time.monotonic() + args.hours * 3600
    iteration = 0
    exit_code = 0

    while True:
        iteration += 1
        block: list[str] = [f"\n[{_ts()}] iteration {iteration}"]
        try:
            block.extend(run_task_flow_lab())
        except Exception as exc:  # noqa: BLE001
            exit_code = 1
            block.append(f"TASK_FLOW_FAIL {type(exc).__name__}: {exc}")
        try:
            block.extend(capture_live_cockpit(args.config, captures))
        except Exception as exc:  # noqa: BLE001
            exit_code = 1
            block.append(f"CAPTURE_FAIL {type(exc).__name__}: {exc}")
        inv_rc, inv_out = run_invariants(args.config)
        if inv_rc != 0:
            exit_code = 1
        block.append(f"invariants_rc={inv_rc}")
        block.append(inv_out.strip())
        text = "\n".join(part for part in block if part)
        print(text, flush=True)
        with log_path.open("a") as handle:
            handle.write(text + "\n")
        if args.once or time.monotonic() >= deadline:
            return exit_code
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
