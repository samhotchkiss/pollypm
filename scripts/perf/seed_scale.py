#!/usr/bin/env python3
"""Generate isolated PollyPM perf-scale fixtures.

The seeder is intentionally outside ``src/pollypm``. It writes a
self-contained workspace/config/transcript fixture and an SQL seed file
that can be applied to an isolated Postgres schema.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class ScaleProfile:
    name: str
    sessions: int
    tasks: int
    messages: int
    large_surface_bytes: int
    browser_note: str


PROFILES = {
    "s": ScaleProfile("s", sessions=4, tasks=25, messages=200, large_surface_bytes=0, browser_note="1 desktop"),
    "m": ScaleProfile("m", sessions=20, tasks=500, messages=5000, large_surface_bytes=1_100_000, browser_note="3 desktop tabs + 1 phone"),
    "l": ScaleProfile("l", sessions=50, tasks=2000, messages=25000, large_surface_bytes=10_500_000, browser_note="10 desktop tabs + 2 phones"),
}

LOCAL_PG_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
DEFAULT_PROJECT = "pollypm"
DEFAULT_ACCOUNT = "codex_primary"


def _keyword_dsn_value(dsn: str, key: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(key)}=('[^']*'|\"[^\"]*\"|\S+)", dsn)
    if match is None:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def is_ambient_live_pg_dsn(dsn: str) -> bool:
    """Return True for the unsafe local production ``pollypm`` DSN."""
    stripped = dsn.strip()
    if "://" in stripped:
        try:
            parsed = urlsplit(stripped)
            port = parsed.port
        except ValueError:
            return False
        db_name = unquote(parsed.path or "").lstrip("/").split("/", 1)[0]
        host = (parsed.hostname or "").lower()
        return (
            db_name == "pollypm"
            and host in LOCAL_PG_HOSTS
            and (port is None or port == 5432)
        )

    db_name = _keyword_dsn_value(stripped, "dbname")
    if db_name != "pollypm":
        return False
    host = (_keyword_dsn_value(stripped, "host") or "").lower()
    port = _keyword_dsn_value(stripped, "port")
    return (
        (host in LOCAL_PG_HOSTS or host.startswith("/"))
        and (port in {None, "", "5432"})
    )


def safe_schema_name(raw: str) -> str:
    value = raw.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise SystemExit(
            "schema must match [A-Za-z_][A-Za-z0-9_]{0,62}; "
            f"got {raw!r}"
        )
    return value


def toml_str(value: object) -> str:
    return json.dumps(str(value))


def sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def json_sql(value: object) -> str:
    return sql_literal(json.dumps(value, sort_keys=True)) + "::jsonb"


def resolve_workspace(raw: str | None, scale: str) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    env = os.environ.get("POLLYPM_PERF_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(tempfile.gettempdir()) / f"pollypm-perf-{scale}"


def default_schema(scale: str) -> str:
    env = os.environ.get("POLLYPM_PERF_SCHEMA", "").strip()
    if env:
        return safe_schema_name(env)
    return f"pollypm_perf_{scale}"


def session_rows(profile: ScaleProfile, workspace: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    roles = ["architect", "advisor"]
    for index in range(profile.sessions):
        if index == 0:
            name = "operator"
            role = "operator-pm"
        else:
            role = roles[(index - 1) % len(roles)]
            name = f"{role}_{index:02d}"
        cwd = workspace / "session-cwds" / name
        rows.append(
            {
                "name": name,
                "role": role,
                "project": DEFAULT_PROJECT,
                "provider": "codex",
                "account": DEFAULT_ACCOUNT,
                "cwd": str(cwd),
                "window_name": name,
            }
        )
    return rows


def task_rows(profile: ScaleProfile) -> list[dict[str, object]]:
    states = ("queued", "in_progress", "review", "done")
    node_by_state = {
        "queued": "implement",
        "in_progress": "implement",
        "review": "code_review",
        "done": None,
    }
    rows: list[dict[str, object]] = []
    for number in range(1, profile.tasks + 1):
        status = states[(number - 1) % len(states)]
        rows.append(
            {
                "project": DEFAULT_PROJECT,
                "task_number": number,
                "title": f"Perf fixture task {number:04d}",
                "type": "task",
                "labels": ["perf-fixture", f"scale:{profile.name}"],
                "work_status": status,
                "flow_template_id": "standard",
                "flow_template_version": 1,
                "current_node_id": node_by_state[status],
                "assignee": "worker" if status in {"queued", "in_progress"} else "reviewer",
                "priority": "normal",
                "description": "Synthetic task created by scripts/perf/seed_scale.py.",
                "roles": {"worker": "worker", "reviewer": "reviewer"},
                "created_by": "perf-seed",
            }
        )
    return rows


def write_config(workspace: Path, rows: list[dict[str, object]], dsn: str | None) -> Path:
    base_dir = workspace / ".pollypm"
    project_root = workspace / "projects" / DEFAULT_PROJECT
    base_dir.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "[project]",
        'name = "PollyPM Perf"',
        f"root_dir = {toml_str(workspace)}",
        'tmux_session = "pollypm-perf"',
        f"workspace_root = {toml_str(workspace)}",
        f"base_dir = {toml_str(base_dir)}",
        f"logs_dir = {toml_str(base_dir / 'logs')}",
        f"snapshots_dir = {toml_str(base_dir / 'snapshots')}",
        f"state_db = {toml_str(base_dir / 'state.db')}",
        "",
        "[pollypm]",
        f'controller_account = "{DEFAULT_ACCOUNT}"',
        'heartbeat_backend = "local"',
        'scheduler_backend = "inline"',
        "",
        "[storage]",
        'backend = "postgres"',
    ]
    if dsn:
        lines.extend(["", "[storage.pg]", f"dsn = {toml_str(dsn)}"])
    lines.extend(
        [
            "",
            f"[accounts.{DEFAULT_ACCOUNT}]",
            'provider = "codex"',
            'runtime = "local"',
            'email = "perf@example.invalid"',
            f"home = {toml_str(base_dir / 'homes' / DEFAULT_ACCOUNT)}",
            "",
            f"[projects.{DEFAULT_PROJECT}]",
            f"path = {toml_str(project_root)}",
            'name = "PollyPM"',
            'kind = "git"',
            "tracked = true",
            "",
        ]
    )
    for row in rows:
        lines.extend(
            [
                f"[sessions.{row['name']}]",
                f"role = {toml_str(row['role'])}",
                f"provider = {toml_str(row['provider'])}",
                f"account = {toml_str(row['account'])}",
                f"cwd = {toml_str(row['cwd'])}",
                f"project = {toml_str(row['project'])}",
                f"window_name = {toml_str(row['window_name'])}",
                "",
            ]
        )
    path = base_dir / "pollypm.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def event_line(
    *,
    timestamp: datetime,
    session_id: str,
    cwd: str,
    event_type: str,
    text: str,
    index: int,
) -> str:
    payload_key = "text" if event_type in {"user_turn", "assistant_turn"} else "message"
    return json.dumps(
        {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "event_type": event_type,
            "session_id": session_id,
            "account_name": DEFAULT_ACCOUNT,
            "provider": "codex",
            "project_key": DEFAULT_PROJECT,
            "source_path": f"perf://{session_id}/{index}",
            "source_offset": index,
            "cwd": cwd,
            "model_name": "perf-fixture",
            "payload": {payload_key: text},
        },
        sort_keys=True,
    )


def write_transcripts(
    workspace: Path,
    profile: ScaleProfile,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    project_root = workspace / "projects" / DEFAULT_PROJECT
    transcripts_root = project_root / ".pollypm" / "transcripts"
    transcripts_root.mkdir(parents=True, exist_ok=True)
    for row in rows:
        Path(str(row["cwd"])).mkdir(parents=True, exist_ok=True)

    remaining = profile.messages
    per_session = [profile.messages // profile.sessions] * profile.sessions
    for index in range(profile.messages % profile.sessions):
        per_session[index] += 1
    max_bytes = 0
    largest_path = ""
    now = datetime.now(UTC) - timedelta(hours=1)
    for idx, row in enumerate(rows):
        session_id = f"perf-{profile.name}-{idx:03d}"
        path = transcripts_root / session_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        count = per_session[idx]
        with path.open("w", encoding="utf-8") as fh:
            for msg_index in range(count):
                event_type = "user_turn" if msg_index % 2 == 0 else "assistant_turn"
                text = f"{profile.name.upper()} fixture message {msg_index} for {row['name']}"
                if idx == 0 and msg_index == count - 1 and profile.large_surface_bytes:
                    text = "LARGE_TRANSCRIPT_BLOCK " + (
                        "x" * profile.large_surface_bytes
                    )
                fh.write(
                    event_line(
                        timestamp=now + timedelta(seconds=remaining),
                        session_id=session_id,
                        cwd=str(row["cwd"]),
                        event_type=event_type,
                        text=text,
                        index=msg_index,
                    )
                    + "\n"
                )
                remaining -= 1
        size = path.stat().st_size
        if size > max_bytes:
            max_bytes = size
            largest_path = str(path)
    return {
        "transcripts_root": str(transcripts_root),
        "messages": profile.messages,
        "largest_events_jsonl": largest_path,
        "largest_events_jsonl_bytes": max_bytes,
    }


def write_sql(
    workspace: Path,
    schema: str,
    profile: ScaleProfile,
    sessions: list[dict[str, object]],
    tasks: list[dict[str, object]],
) -> Path:
    path = workspace / ".pollypm" / f"seed_{profile.name}.sql"
    snapshots = workspace / ".pollypm" / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    lines = [
        f'CREATE SCHEMA IF NOT EXISTS "{schema}";',
        f'SET search_path TO "{schema}", public;',
        "BEGIN;",
        "",
        "INSERT INTO work_flow_templates (name, version, description, roles, start_node, is_current, created_at)",
        "VALUES ('standard', 1, 'Perf fixture standard flow', '{\"worker\":\"worker\",\"reviewer\":\"reviewer\"}'::jsonb, 'implement', true, now())",
        "ON CONFLICT (name, version) DO NOTHING;",
        "INSERT INTO work_flow_nodes (flow_template_name, flow_template_version, node_id, name, type, actor_type, actor_role, next_node_id, reject_node_id, gates)",
        "VALUES",
        "  ('standard', 1, 'implement', 'Implement', 'work', 'agent', 'worker', 'code_review', NULL, '[]'::jsonb),",
        "  ('standard', 1, 'code_review', 'Code review', 'review', 'agent', 'reviewer', 'done', 'implement', '[]'::jsonb),",
        "  ('standard', 1, 'done', 'Done', 'terminal', NULL, NULL, NULL, NULL, '[]'::jsonb)",
        "ON CONFLICT (flow_template_name, flow_template_version, node_id) DO NOTHING;",
        "",
    ]
    for row in sessions:
        snapshot_path = snapshots / f"{row['name']}.txt"
        snapshot_path.write_text("perf fixture snapshot\n", encoding="utf-8")
        lines.extend(
            [
                "INSERT INTO sessions (name, role, project, provider, account, cwd, window_name)",
                "VALUES ("
                + ", ".join(
                    sql_literal(row[key])
                    for key in ("name", "role", "project", "provider", "account", "cwd", "window_name")
                )
                + ")",
                "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, project = EXCLUDED.project, provider = EXCLUDED.provider, account = EXCLUDED.account, cwd = EXCLUDED.cwd, window_name = EXCLUDED.window_name;",
                "INSERT INTO session_runtime (session_name, status, effective_account, effective_provider, recovery_attempts, updated_at)",
                f"VALUES ({sql_literal(row['name'])}, 'healthy', {sql_literal(row['account'])}, {sql_literal(row['provider'])}, 0, now())",
                "ON CONFLICT (session_name) DO UPDATE SET status = EXCLUDED.status, effective_account = EXCLUDED.effective_account, effective_provider = EXCLUDED.effective_provider, updated_at = EXCLUDED.updated_at;",
                "INSERT INTO heartbeats (session_name, tmux_window, pane_id, pane_command, pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at)",
                f"VALUES ({sql_literal(row['name'])}, {sql_literal(row['window_name'])}, '0', 'perf-fixture', false, 0, {sql_literal(snapshot_path)}, 'perf', now());",
            ]
        )
    lines.append("")
    for task in tasks:
        lines.append(
            "INSERT INTO work_tasks (project, task_number, project_key, title, type, labels, work_status, flow_template_id, flow_template_version, current_node_id, assignee, priority, description, relevant_files, roles, external_refs, created_at, created_by, updated_at) "
            "VALUES ("
            + ", ".join(
                [
                    sql_literal(task["project"]),
                    str(task["task_number"]),
                    sql_literal(task["project"]),
                    sql_literal(task["title"]),
                    sql_literal(task["type"]),
                    json_sql(task["labels"]),
                    sql_literal(task["work_status"]),
                    sql_literal(task["flow_template_id"]),
                    str(task["flow_template_version"]),
                    "NULL" if task["current_node_id"] is None else sql_literal(task["current_node_id"]),
                    sql_literal(task["assignee"]),
                    sql_literal(task["priority"]),
                    sql_literal(task["description"]),
                    "'[]'::jsonb",
                    json_sql(task["roles"]),
                    "'{}'::jsonb",
                    sql_literal(now),
                    sql_literal(task["created_by"]),
                    sql_literal(now),
                ]
            )
            + ") ON CONFLICT (project, task_number) DO UPDATE SET title = EXCLUDED.title, labels = EXCLUDED.labels, work_status = EXCLUDED.work_status, current_node_id = EXCLUDED.current_node_id, assignee = EXCLUDED.assignee, updated_at = EXCLUDED.updated_at;"
        )
    lines.extend(
        [
            "",
            "INSERT INTO token_usage_hourly (hour_bucket, account_name, provider, model_name, project_key, tokens_used, updated_at)",
            f"VALUES ({sql_literal(datetime.now(UTC).strftime('%Y-%m-%dT%H:00:00Z'))}, {sql_literal(DEFAULT_ACCOUNT)}, 'codex', 'perf-fixture', {sql_literal(DEFAULT_PROJECT)}, {profile.messages * 42}, now())",
            "ON CONFLICT (hour_bucket, account_name, provider, model_name, project_key) DO UPDATE SET tokens_used = EXCLUDED.tokens_used, updated_at = EXCLUDED.updated_at;",
            "COMMIT;",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_psql(dsn: str, sql_path: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        raise SystemExit("psql not found on PATH; install PostgreSQL client tools")
    subprocess.run([psql, dsn, "-v", "ON_ERROR_STOP=1", "-f", str(sql_path)], check=True)


def seed(args: argparse.Namespace) -> dict[str, object]:
    profile = PROFILES[args.scale]
    dsn = args.dsn or os.environ.get("POLLYPM_PERF_PG_DSN") or os.environ.get("POLLYPM_PG_DSN")
    if args.execute:
        if not dsn:
            raise SystemExit("seed --execute requires --dsn or POLLYPM_PERF_PG_DSN")
        if is_ambient_live_pg_dsn(dsn):
            raise SystemExit(
                "refusing to seed the ambient local pollypm database; "
                "use an isolated test database or non-default port"
            )
    schema = safe_schema_name(args.schema or default_schema(args.scale))
    workspace = resolve_workspace(args.workspace, args.scale)
    if args.force_clean and workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    sessions = session_rows(profile, workspace)
    tasks = task_rows(profile)
    config_path = write_config(workspace, sessions, dsn)
    transcript_summary = write_transcripts(workspace, profile, sessions)
    sql_path = write_sql(workspace, schema, profile, sessions, tasks)
    manifest = {
        "scale": asdict(profile),
        "workspace": str(workspace),
        "config_path": str(config_path),
        "schema": schema,
        "sql_path": str(sql_path),
        "sessions": len(sessions),
        "tasks": len(tasks),
        "transcripts": transcript_summary,
        "execute": bool(args.execute),
        "generated_at": datetime.now(UTC).isoformat(),
        "verify_commands": [
            f"POLLYPM_CONFIG={config_path} pm chat sessions --json",
            f"POLLYPM_CONFIG={config_path} pm task list --project {DEFAULT_PROJECT} --status all --json",
        ],
    }
    manifest_path = workspace / ".pollypm" / f"seed_{profile.name}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    if args.execute:
        run_psql(dsn, sql_path)
    return manifest


def teardown(args: argparse.Namespace) -> dict[str, object]:
    dsn = args.dsn or os.environ.get("POLLYPM_PERF_PG_DSN") or os.environ.get("POLLYPM_PG_DSN")
    schema = safe_schema_name(args.schema or default_schema(args.scale))
    workspace = resolve_workspace(args.workspace, args.scale)
    if dsn:
        if is_ambient_live_pg_dsn(dsn):
            raise SystemExit("refusing to modify the ambient local pollypm database")
        psql = shutil.which("psql")
        if psql is not None:
            subprocess.run(
                [psql, dsn, "-v", "ON_ERROR_STOP=1", "-c", f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;'],
                check=True,
            )
    if workspace.exists() and args.remove_workspace:
        shutil.rmtree(workspace)
    return {"schema": schema, "workspace": str(workspace), "removed_workspace": args.remove_workspace}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "teardown"):
        p = sub.add_parser(name)
        p.add_argument("--scale", choices=sorted(PROFILES), required=True)
        p.add_argument("--workspace")
        p.add_argument("--schema")
        p.add_argument("--dsn")
        if name == "seed":
            p.add_argument("--execute", action="store_true", help="Apply the generated SQL with psql")
            p.add_argument("--force-clean", action="store_true", help="Remove the workspace before seeding")
            p.add_argument("--json-out")
        else:
            p.add_argument("--remove-workspace", action="store_true", default=True)
            p.add_argument("--keep-workspace", action="store_false", dest="remove_workspace")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "seed":
        result = seed(args)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        result = teardown(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
