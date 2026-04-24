#!/usr/bin/env python3
"""Release burn-in invariant checks for live PollyPM workspaces.

This is intentionally a harness, not product code. It reads public config,
workspace stores, project work-service DBs, and tmux captures to catch UX/task
flow regressions during pre-release burn-in.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TERMINAL = {"done", "accepted", "cancelled", "canceled"}
KNOWN_STATUSES = {
    "draft",
    "queued",
    "in_progress",
    "review",
    "blocked",
    "on_hold",
    "waiting_on_user",
    *TERMINAL,
}
ACTION_MESSAGE_TYPES = {"notify", "inbox_task", "alert"}


@dataclass(slots=True)
class Finding:
    severity: str
    code: str
    detail: str


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _task_id(row: sqlite3.Row) -> str:
    return f"{row['project']}/{row['task_number']}"


def _project_db(project: Any) -> Path | None:
    path = getattr(project, "path", None)
    if not isinstance(path, Path):
        return None
    db = path / ".pollypm" / "state.db"
    return db if db.exists() else None


def _project_path(project: Any) -> Path | None:
    path = getattr(project, "path", None)
    return path if isinstance(path, Path) else None


def _load_dashboard_body(config_path: Path, project_key: str) -> tuple[str, str]:
    from pollypm.cockpit_ui import PollyProjectDashboardApp, _gather_project_dashboard

    data = _gather_project_dashboard(config_path, project_key)
    if data is None:
        return "", ""
    app = PollyProjectDashboardApp(config_path, project_key)
    return str(data.status_label), app._render_inbox_body(data)


def check_project_tasks(
    *,
    config_path: Path,
    project_key: str,
    project: Any,
) -> list[Finding]:
    findings: list[Finding] = []
    project_path = _project_path(project)
    if project_path is None:
        return [
            Finding(
                "warn",
                "project_path_invalid",
                f"{project_key}: no valid project path configured",
            )
        ]
    if not project_path.exists():
        return [
            Finding(
                "warn",
                "project_path_missing",
                f"{project_key}: configured path does not exist: {project_path}",
            )
        ]
    db = _project_db(project)
    if db is None:
        # A registered project may not have a work-service DB until its
        # first task is created. Absence alone is not a task-flow risk.
        return []

    try:
        conn = _connect(db)
    except sqlite3.Error as exc:
        return [Finding("fail", "project_db_open_failed", f"{project_key}: {exc}")]

    with conn:
        if not _table_exists(conn, "work_tasks"):
            return []
        tasks = conn.execute(
            "SELECT * FROM work_tasks WHERE project=? ORDER BY task_number",
            (project_key,),
        ).fetchall()
        if not tasks:
            return []

        blocked_count = 0
        for row in tasks:
            task_id = _task_id(row)
            status = str(row["work_status"] or "")
            labels = set(str(label) for label in _json_list(row["labels"]))
            if status not in KNOWN_STATUSES:
                findings.append(
                    Finding("fail", "unknown_task_status", f"{task_id}: {status!r}")
                )
            if status == "in_progress" and not str(row["assignee"] or "").strip():
                findings.append(
                    Finding("fail", "in_progress_unassigned", f"{task_id}: no assignee")
                )
            if status in {"blocked", "on_hold", "waiting_on_user"}:
                blocked_count += 1
                context = conn.execute(
                    "SELECT 1 FROM work_context_entries "
                    "WHERE task_project=? AND task_number=? LIMIT 1",
                    (project_key, row["task_number"]),
                ).fetchone()
                deps = conn.execute(
                    "SELECT 1 FROM work_task_dependencies "
                    "WHERE from_project=? AND from_task_number=? LIMIT 1",
                    (project_key, row["task_number"]),
                ).fetchone()
                if context is None and deps is None:
                    findings.append(
                        Finding(
                            "warn",
                            "blocked_without_context",
                            f"{task_id}: {status} has no context or dependency row",
                        )
                    )
            if (
                "review_feedback" in labels
                and "user" not in _json_dict(row["roles"]).values()
                and "user" not in _json_dict(row["roles"])
            ):
                findings.append(
                    Finding(
                        "fail",
                        "review_feedback_not_user_assigned",
                        f"{task_id}: review feedback is not assigned to the user inbox",
                    )
                )
            if status == "review":
                columns = {
                    str(info["name"])
                    for info in conn.execute("PRAGMA table_info(work_node_executions)")
                }
                output_column = (
                    "work_output" if "work_output" in columns else "output_json"
                )
                output = conn.execute(
                    f"SELECT 1 FROM work_node_executions "
                    f"WHERE task_project=? AND task_number=? "
                    f"AND {output_column} IS NOT NULL LIMIT 1",
                    (project_key, row["task_number"]),
                ).fetchone()
                if output is None:
                    findings.append(
                        Finding(
                            "warn",
                            "review_without_artifact",
                            f"{task_id}: review state has no output artifact",
                        )
                    )

        if blocked_count:
            status_label, body = _load_dashboard_body(config_path, project_key)
            body_lower = body.lower()
            if status_label not in {"blocked", "needs attention", "active"}:
                findings.append(
                    Finding(
                        "fail",
                        "blocked_project_status_not_visible",
                        f"{project_key}: blocked tasks but dashboard status is {status_label!r}",
                    )
                )
            if "action required" not in body_lower and "summary missing" not in body_lower:
                findings.append(
                    Finding(
                        "fail",
                        "blocked_project_no_action_copy",
                        f"{project_key}: blocked tasks but dashboard has no action copy",
                    )
                )

    return findings


def _message_project_refs(row: sqlite3.Row) -> set[str]:
    refs: set[str] = set()
    payload = _json_dict(row["payload_json"])
    for key in ("project", "task_project"):
        value = payload.get(key)
        if isinstance(value, str) and value and value not in {"inbox", "workspace", "global"}:
            refs.add(value)
    labels = _json_list(row["labels"])
    for label in labels:
        if isinstance(label, str) and label.startswith("project:"):
            project = label.split(":", 1)[1]
            if project not in {"inbox", "workspace", "global"}:
                refs.add(project)
    scope = str(row["scope"] or "")
    if scope and scope not in {"inbox", "cockpit", "workspace", "global"}:
        refs.add(scope)
    return refs


def check_workspace_messages(config: Any) -> list[Finding]:
    findings: list[Finding] = []
    workspace_root = getattr(config.project, "workspace_root", None)
    if workspace_root is None:
        return []
    db = Path(workspace_root) / ".pollypm" / "state.db"
    if not db.exists():
        return []
    known_projects = set(getattr(config, "projects", {}) or {})
    try:
        conn = _connect(db)
    except sqlite3.Error as exc:
        return [Finding("fail", "workspace_db_open_failed", str(exc))]
    with conn:
        if not _table_exists(conn, "messages"):
            return []
        rows = conn.execute(
            "SELECT * FROM messages WHERE state='open' "
            "AND recipient='user' AND type IN ('notify','inbox_task','alert')"
        ).fetchall()
    for row in rows:
        refs = _message_project_refs(row)
        stale = sorted(ref for ref in refs if ref not in known_projects)
        if stale:
            findings.append(
                Finding(
                    "warn",
                    "action_message_for_unknown_project",
                    f"message {row['id']}: raw store row references deleted/unknown project(s) {', '.join(stale)}; inbox should quarantine it",
                )
            )
    return findings


def check_cockpit_tmux(config: Any) -> list[Finding]:
    findings: list[Finding] = []
    session = config.project.tmux_session
    state_path = Path.home() / ".pollypm" / "cockpit_state.json"
    if not state_path.exists():
        return []
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [Finding("warn", "cockpit_state_unreadable", str(exc))]
    selected = str(state.get("selected") or "")
    if not selected.startswith("project:") or not selected.endswith(":dashboard"):
        return []
    project_key = selected.split(":", 2)[1]
    project = getattr(config, "projects", {}).get(project_key)
    if project is None:
        return [
            Finding(
                "fail",
                "selected_project_unknown",
                f"cockpit selected {project_key}, but config has no project",
            )
        ]
    expected = project.display_label() if hasattr(project, "display_label") else project_key
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session}:0.1", "-p", "-S", "-80"],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [Finding("warn", "cockpit_capture_failed", str(exc))]
    if out.returncode != 0:
        return [Finding("warn", "cockpit_capture_failed", out.stderr.strip())]
    if expected not in out.stdout:
        findings.append(
            Finding(
                "fail",
                "rail_detail_mismatch",
                f"selected {selected} but right pane does not contain {expected!r}",
            )
        )
    if project_key in {"polly_remote", "notesy"} and "Action Needed" not in out.stdout:
        findings.append(
            Finding(
                "fail",
                "notesy_missing_action_needed",
                "Notesy dashboard does not show Action Needed in right pane",
            )
        )
    return findings


def run_checks(config_path: Path) -> list[Finding]:
    from pollypm.config import load_config

    config = load_config(config_path)
    findings: list[Finding] = []
    for project_key, project in (getattr(config, "projects", {}) or {}).items():
        findings.extend(
            check_project_tasks(
                config_path=config_path,
                project_key=str(project_key),
                project=project,
            )
        )
    findings.extend(check_workspace_messages(config))
    findings.extend(check_cockpit_tmux(config))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".pollypm" / "pollypm.toml",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings = run_checks(args.config)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "severity": finding.severity,
                        "code": finding.code,
                        "detail": finding.detail,
                    }
                    for finding in findings
                ],
                indent=2,
            )
        )
    else:
        if not findings:
            print("release invariants: ok")
        for finding in findings:
            print(f"{finding.severity.upper()} {finding.code}: {finding.detail}")
    return 1 if any(f.severity == "fail" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
