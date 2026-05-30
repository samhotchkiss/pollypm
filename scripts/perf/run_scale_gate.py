#!/usr/bin/env python3
"""Run an isolated PollyPM scale perf gate.

The runner builds the fixture workspace, applies migrations, seeds the
requested scale into an isolated Postgres schema, starts a private Web API
server on a non-production port, measures the configured HTTP scenarios,
then tears the schema/server/workspace back down.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import seed_scale  # noqa: E402


DEFAULT_SCENARIOS = "dashboard,sessions,messages,task-list,task-detail,inbox"
DEFAULT_PERF_DB = "pollypm_perf"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SERVER_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class ServerProcess:
    process: subprocess.Popen[str]
    log_file: object
    log_path: Path


def safe_database_name(raw: str) -> str:
    value = raw.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise SystemExit(
            "database name must match [A-Za-z_][A-Za-z0-9_]{0,62}; "
            f"got {raw!r}"
        )
    return value


def default_perf_dsn() -> str:
    env_dsn = os.environ.get("POLLYPM_PERF_PG_DSN", "").strip()
    if env_dsn:
        return env_dsn
    db_name = safe_database_name(
        os.environ.get("POLLYPM_PERF_DB", DEFAULT_PERF_DB)
    )
    return f"postgresql://localhost:5432/{db_name}"


def require_url_dsn(dsn: str) -> str:
    stripped = dsn.strip()
    if "://" not in stripped:
        raise SystemExit(
            "perf runner requires a URL-style Postgres DSN "
            "(for example postgresql://localhost:5432/pollypm_perf)"
        )
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise SystemExit(f"perf runner requires a Postgres DSN, got {dsn!r}")
    db_name = database_name(stripped)
    safe_database_name(db_name)
    return stripped


def database_name(dsn: str) -> str:
    parsed = urlsplit(dsn)
    value = unquote(parsed.path or "").lstrip("/").split("/", 1)[0]
    if not value:
        raise SystemExit(f"Postgres DSN must include a database name: {dsn!r}")
    return value


def dsn_with_database(dsn: str, db_name: str) -> str:
    safe_db = safe_database_name(db_name)
    parsed = urlsplit(dsn)
    path = f"/{safe_db}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def dsn_without_options(dsn: str) -> str:
    parsed = urlsplit(dsn)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "options"
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def admin_dsn_for(dsn: str) -> str:
    return dsn_with_database(dsn_without_options(dsn), "postgres")


def ensure_not_ambient(dsn: str) -> None:
    if seed_scale.is_ambient_live_pg_dsn(dsn):
        raise SystemExit(
            "refusing to run perf gate against the ambient local pollypm "
            "database; use POLLYPM_PERF_PG_DSN or POLLYPM_PERF_DB"
        )


def ensure_database(dsn: str) -> bool:
    """Create the target database when missing.

    Returns True when a database was created, False when it already existed.
    """
    ensure_not_ambient(dsn)
    import psycopg
    from psycopg import sql

    db_name = safe_database_name(database_name(dsn))
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return False
    except Exception:
        pass

    with psycopg.connect(admin_dsn_for(dsn), connect_timeout=5) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,),
            )
            if cur.fetchone() is not None:
                return False
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
            )
    return True


def create_schema(dsn: str, schema: str) -> None:
    ensure_not_ambient(dsn)
    import psycopg
    from psycopg import sql

    safe_schema = seed_scale.safe_schema_name(schema)
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(safe_schema)
                )
            )
        conn.commit()


def drop_schema(dsn: str, schema: str) -> None:
    ensure_not_ambient(dsn)
    import psycopg
    from psycopg import sql

    safe_schema = seed_scale.safe_schema_name(schema)
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(safe_schema)
                )
            )
        conn.commit()


def apply_seed_sql(dsn: str, sql_path: Path) -> None:
    ensure_not_ambient(dsn)
    import psycopg

    sql_text = sql_path.read_text(encoding="utf-8")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql_text)


def child_env(config_path: Path, dsn: str, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_parts = [str(SRC_DIR)]
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["POLLYPM_CONFIG"] = str(config_path)
    env["POLLYPM_PG_DSN"] = dsn
    env["POLLYPM_PERF_PG_DSN"] = dsn
    env["POLLYPM_ERROR_LOG_PATH"] = str(workspace / ".pollypm" / "errors.log")
    env["POLLYPM_DISABLE_ERROR_NOTIFICATIONS"] = "1"
    return env


def parse_pm_command(raw: str | None) -> list[str]:
    if raw:
        return shlex.split(raw)
    env_command = os.environ.get("POLLYPM_PERF_PM_COMMAND", "").strip()
    if env_command:
        return shlex.split(env_command)
    if shutil.which("uv") is not None:
        return ["uv", "run", "pm"]
    return ["pm"]


def choose_port(raw_port: int | None) -> int:
    if raw_port:
        if raw_port == 8765:
            raise SystemExit("refusing to run the perf server on live port 8765")
        return raw_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


def run_workspace_migrations(
    pm_command: list[str],
    config_path: Path,
    env: dict[str, str],
) -> None:
    subprocess.run(
        [
            *pm_command,
            "migrate",
            "--apply",
            "--force",
            "--config",
            str(config_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )


def run_pg_migrations(config_path: Path, env: dict[str, str]) -> None:
    old_values = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        from pollypm.config import load_config
        from pollypm.storage.pg_migrations import apply_migrations
        from pollypm.storage.pg_pool import get_rw_pool, pg_pool_shutdown

        config = load_config(config_path)
        try:
            apply_migrations(get_rw_pool(config))
        finally:
            pg_pool_shutdown()
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def start_server(
    pm_command: list[str],
    *,
    config_path: Path,
    token_path: Path,
    port: int,
    env: dict[str, str],
    workspace: Path,
) -> ServerProcess:
    log_path = workspace / ".pollypm" / "perf_serve.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            *pm_command,
            "serve",
            "--host",
            DEFAULT_HOST,
            "--port",
            str(port),
            "--config",
            str(config_path),
            "--token-path",
            str(token_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return ServerProcess(process=process, log_file=log_file, log_path=log_path)


def stop_server(server: ServerProcess | None) -> None:
    if server is None:
        return
    proc = server.process
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
    try:
        server.log_file.close()
    except Exception:
        pass


def wait_for_health(base_url: str, server: ServerProcess, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    health_url = f"{base_url}/api/v1/health"
    last_error = ""
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            raise SystemExit(
                "perf server exited before health check passed; "
                f"see {server.log_path}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                if int(response.status) == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise SystemExit(
        f"perf server did not become healthy within {timeout:.1f}s "
        f"({last_error}); see {server.log_path}"
    )


def read_token(token_path: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        if token:
            return token
        time.sleep(0.1)
    raise SystemExit(f"perf server did not write token at {token_path}")


def warmup_dashboard(base_url: str, token: str, timeout: float) -> None:
    request = urllib.request.Request(
        f"{base_url}/api/v1/dashboard",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "pollypm-perf-gate/1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
        if int(response.status) < 200 or int(response.status) >= 300:
            raise SystemExit(f"dashboard warmup returned HTTP {response.status}")


def run_measurement(
    args: argparse.Namespace,
    *,
    base_url: str,
    token_path: Path,
    env: dict[str, str],
    workspace: Path,
) -> dict[str, str]:
    report_dir = workspace / ".pollypm" / "reports"
    json_out = Path(args.json_out) if args.json_out else report_dir / "perf.json"
    markdown_out = (
        Path(args.markdown_out) if args.markdown_out else report_dir / "perf.md"
    )
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "measure_http.py"),
        "measure",
        "--base",
        base_url,
        "--token-file",
        str(token_path),
        "--scenarios",
        args.scenarios,
        "--samples",
        str(args.samples),
        "--timeout",
        str(args.request_timeout),
        "--json-out",
        str(json_out),
        "--markdown-out",
        str(markdown_out),
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=True)
    return {"json": str(json_out), "markdown": str(markdown_out)}


def remove_workspace(workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)


def run_gate(args: argparse.Namespace) -> dict[str, object]:
    scale = args.scale
    schema = seed_scale.safe_schema_name(args.schema or seed_scale.default_schema(scale))
    workspace = seed_scale.resolve_workspace(args.workspace, scale)
    raw_dsn = require_url_dsn(args.dsn or default_perf_dsn())
    ensure_not_ambient(raw_dsn)
    app_dsn = seed_scale.dsn_with_search_path(raw_dsn, schema)
    ensure_not_ambient(app_dsn)
    port = choose_port(args.port)
    pm_command = parse_pm_command(args.pm_command)
    server: ServerProcess | None = None
    created_database = False
    schema_created = False
    reports: dict[str, str] = {}

    try:
        created_database = ensure_database(raw_dsn)
        create_schema(raw_dsn, schema)
        schema_created = True
        manifest = seed_scale.seed(
            Namespace(
                scale=scale,
                dsn=raw_dsn,
                execute=False,
                schema=schema,
                workspace=str(workspace),
                force_clean=True,
                json_out=None,
            )
        )
        config_path = Path(str(manifest["config_path"]))
        token_path = workspace / ".pollypm" / "api-token"
        env = child_env(config_path, app_dsn, workspace)
        run_workspace_migrations(pm_command, config_path, env)
        run_pg_migrations(config_path, env)
        apply_seed_sql(app_dsn, Path(str(manifest["sql_path"])))

        server = start_server(
            pm_command,
            config_path=config_path,
            token_path=token_path,
            port=port,
            env=env,
            workspace=workspace,
        )
        base_url = f"http://{DEFAULT_HOST}:{port}"
        wait_for_health(base_url, server, args.server_timeout)
        token = read_token(token_path, args.server_timeout)
        if args.warmup_dashboard:
            warmup_dashboard(base_url, token, args.request_timeout)
        reports = run_measurement(
            args,
            base_url=base_url,
            token_path=token_path,
            env=env,
            workspace=workspace,
        )
        return {
            "mode": "perf-scale-gate",
            "scale": scale,
            "schema": schema,
            "database": database_name(raw_dsn),
            "database_created": created_database,
            "base": base_url,
            "workspace": str(workspace),
            "workspace_removed": not args.keep_workspace,
            "reports": reports,
            "warmup_dashboard": bool(args.warmup_dashboard),
        }
    finally:
        stop_server(server)
        try:
            if schema_created:
                drop_schema(raw_dsn, schema)
        finally:
            if not args.keep_workspace:
                remove_workspace(workspace)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=sorted(seed_scale.PROFILES), default="m")
    parser.add_argument("--workspace", default=os.environ.get("POLLYPM_PERF_WORKSPACE"))
    parser.add_argument("--schema", default=os.environ.get("POLLYPM_PERF_SCHEMA"))
    parser.add_argument(
        "--dsn",
        default=os.environ.get("POLLYPM_PERF_PG_DSN"),
        help=(
            "URL-style Postgres DSN for the isolated perf DB. Defaults to "
            "postgresql://localhost:5432/$POLLYPM_PERF_DB or pollypm_perf."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=(
            int(os.environ["POLLYPM_PERF_PORT"])
            if os.environ.get("POLLYPM_PERF_PORT")
            else None
        ),
        help="Perf server port. Defaults to an ephemeral non-8765 port.",
    )
    parser.add_argument(
        "--pm-command",
        help="Command used to invoke pm. Defaults to POLLYPM_PERF_PM_COMMAND, uv run pm, then pm.",
    )
    parser.add_argument(
        "--scenarios",
        default=os.environ.get("POLLYPM_PERF_SCENARIOS", DEFAULT_SCENARIOS),
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=int(os.environ.get("POLLYPM_PERF_SAMPLES", "30")),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.environ.get("POLLYPM_PERF_REQUEST_TIMEOUT", "30")),
    )
    parser.add_argument(
        "--server-timeout",
        type=float,
        default=float(
            os.environ.get(
                "POLLYPM_PERF_SERVER_TIMEOUT",
                str(DEFAULT_SERVER_TIMEOUT_SECONDS),
            )
        ),
    )
    parser.add_argument("--json-out", help="Write measurement JSON here.")
    parser.add_argument("--markdown-out", help="Write measurement Markdown here.")
    parser.add_argument(
        "--warmup-dashboard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prime the dashboard snapshot once before measured warm samples.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the generated workspace/reports for debugging.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress JSON summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_gate(args)
    if not args.quiet:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
