"""``pm storage`` CLI — Postgres migration + bootstrap tooling.

Two operator-facing entry points live here:

* ``pm storage migrate-to-pg`` (issue #1737, Slice E) — copies an
  existing sqlite workspace into the pg schema. The implementation
  lives in :mod:`pollypm.storage.pg_migration_tool`.
* ``pm bootstrap-pg`` (issue #1747) — guided one-shot install for
  first-run operators on macOS Homebrew. Detects the platform, runs
  ``brew install postgresql@17 pgvector``, starts the service,
  ``createdb pollypm``, ``CREATE EXTENSION vector``, applies the
  schema, and writes a ``[storage]`` block to ``~/.pollypm/pollypm.toml``.
  Every destructive step is gated behind ``--yes`` (default is dry-run).

Both commands share the typer-level glue: flag parsing, confirmation
prompts, structured failure messages.

Contract
--------

- Inputs: ``--dsn``, ``--from-sqlite``, ``--include-legacy-per-project``,
  ``--reembed``, ``--dry-run`` (default), ``--commit`` (kill switch),
  ``--yes`` (skip confirmation), plus the standard ``--config``.
- Outputs: human-readable progress + per-source summary on stdout,
  structured failure messages on stderr.
- Exit codes:
    0 — success (every source either copied or already imported)
    1 — partial / total failure (at least one source failed)
    2 — bad flags / config not found
    3 — pre-flight check failed (pg unreachable, schema missing, etc.)
- Side effects: mutates pg only when ``--commit`` is set. Renames each
  source ``state.db`` to ``state.db.pre-pg-<ts>`` on success. The audit
  table ``_pg_migration_audit`` is created on first run and consulted
  for idempotency.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


__all__ = [
    "bootstrap_pg",
    "register_storage_commands",
    "scan_pollypm_home",
    "storage_app",
]


# --------------------------------------------------------------------- #
# ``pm bootstrap-pg`` — guided one-shot Postgres install (#1747).
# --------------------------------------------------------------------- #
#
# Design notes
# ------------
#
# The command is a thin orchestrator over `brew`, `createdb`, `psql`,
# and our own `apply_migrations` / config writer. It is **never silent
# about mutation**: --dry-run is the default; the destructive path
# requires --yes (and we still print every command before running it).
#
# Step list (each step is idempotent on its own and skip-when-satisfied):
#
#   1. Detect platform (macOS Homebrew is the only fully-supported
#      target for now; Linux falls back to a copy-paste hint).
#   2. `brew install postgresql@17 pgvector` (skip per-package if
#      already installed).
#   3. `brew services start postgresql@17` (skip if already running).
#   4. `createdb pollypm` (skip if the db already exists).
#   5. `CREATE EXTENSION IF NOT EXISTS vector` via psql — this also
#      catches the pgvector-missing case before apply_migrations does.
#   6. `apply_migrations` against the new pg, surfacing the friendly
#      pgvector error from #1750 if the extension is somehow still
#      not visible.
#   7. Write a `[storage]` block to `~/.pollypm/pollypm.toml` so the
#      runtime picks up the new DSN on next launch.


_DEFAULT_PG_VERSION = "postgresql@17"
_DEFAULT_DB_NAME = "pollypm"
_DEFAULT_DSN = f"postgresql://localhost:5432/{_DEFAULT_DB_NAME}"


@dataclass(slots=True)
class _Step:
    """One step of the bootstrap pipeline.

    Each step records the human label, the command to run (``None`` =
    pure-Python step), and whether it's a no-op on this system.
    """

    label: str
    cmd: list[str] | None
    skip_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skip_reason)


def _which(binary: str) -> str | None:
    """Resolve ``binary`` on PATH; return None if not found."""
    return shutil.which(binary)


def _brew_pkg_installed(pkg: str) -> bool:
    """Return True if ``brew list <pkg>`` succeeds (best-effort)."""
    brew = _which("brew")
    if brew is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            [brew, "list", "--versions", pkg],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _brew_service_running(svc: str) -> bool:
    """Return True if ``brew services list`` reports ``svc`` running."""
    brew = _which("brew")
    if brew is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [brew, "services", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == svc and parts[1].lower() == "started":
            return True
    return False


def _pg_db_exists(db_name: str) -> bool:
    """Return True if the local Postgres reports ``db_name`` already created.

    Uses psycopg with a parameter-bound ``%s`` placeholder against the
    ``postgres`` admin database. The previous implementation f-string'd
    ``db_name`` directly into a SQL literal passed to ``psql -c`` — that
    would let a hostile ``--db`` value inject SQL against the local
    superuser session (#1890). Parameter binding makes the input data,
    not code, regardless of what characters it contains.

    Returns False on any connection / import / runtime error: the
    bootstrap planner treats False as "assume the db doesn't exist
    yet, plan a createdb step" which is the safe default for first run.
    """
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(
            "postgresql://localhost:5432/postgres",
            connect_timeout=5,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (db_name,),
                )
                row = cur.fetchone()
                return row is not None and row[0] == 1
    except Exception:  # noqa: BLE001 — any failure means "unknown, plan createdb"
        return False


def _plan_steps(
    *,
    pg_version: str,
    db_name: str,
) -> list[_Step]:
    """Compute the per-platform bootstrap plan, marking skipped steps.

    macOS / Homebrew is the only fully-orchestrated target. On every
    other platform we still print the plan, but every brew step is
    marked skipped with a hint pointing at the platform docs.
    """
    is_mac = platform.system() == "Darwin"
    brew_available = _which("brew") is not None

    steps: list[_Step] = []

    if not (is_mac and brew_available):
        # Non-Homebrew systems get a single informational step that
        # tells the operator exactly which manual install to run. We
        # still try the createdb + extension + migrations + config
        # write steps below — those work against any reachable pg.
        steps.append(
            _Step(
                label=f"Install {pg_version} and pgvector",
                cmd=None,
                skip_reason=(
                    "Non-Homebrew platform — install Postgres 16+ and "
                    "pgvector via your system package manager, then "
                    "re-run `pm bootstrap-pg` (subsequent steps will "
                    "be picked up). See https://github.com/pgvector/"
                    "pgvector#installation-notes."
                ),
            )
        )
    else:
        pg_installed = _brew_pkg_installed(pg_version)
        steps.append(
            _Step(
                label=f"brew install {pg_version}",
                cmd=["brew", "install", pg_version],
                skip_reason=(
                    f"{pg_version} already installed (brew list)"
                    if pg_installed
                    else ""
                ),
            )
        )
        pgvector_installed = _brew_pkg_installed("pgvector")
        steps.append(
            _Step(
                label="brew install pgvector",
                cmd=["brew", "install", "pgvector"],
                skip_reason=(
                    "pgvector already installed (brew list)"
                    if pgvector_installed
                    else ""
                ),
            )
        )
        svc_running = _brew_service_running(pg_version)
        steps.append(
            _Step(
                label=f"brew services start {pg_version}",
                cmd=["brew", "services", "start", pg_version],
                skip_reason=(
                    f"{pg_version} service already running"
                    if svc_running
                    else ""
                ),
            )
        )

    db_exists = _pg_db_exists(db_name)
    steps.append(
        _Step(
            label=f"createdb {db_name}",
            cmd=["createdb", db_name],
            skip_reason=(
                f"database {db_name!r} already exists" if db_exists else ""
            ),
        )
    )
    # Pure-Python step: routed through ``_create_vector_extension`` so a
    # missing pgvector contrib package surfaces the same friendly hint
    # ``apply_migrations`` would raise (issue #1889). The previous
    # implementation shelled out to ``psql -c`` here, which bypassed
    # the ``PgVectorExtensionMissing`` wrapper entirely.
    steps.append(
        _Step(
            label="CREATE EXTENSION IF NOT EXISTS vector",
            cmd=None,
        )
    )
    steps.append(
        _Step(
            label="apply_migrations (pollypm schema)",
            cmd=None,  # pure-Python — handled inline below
        )
    )
    steps.append(
        _Step(
            label="Write [storage] block to ~/.pollypm/pollypm.toml",
            cmd=None,
        )
    )
    return steps


def _print_plan(steps: list[_Step], *, dry_run: bool) -> None:
    """Render the per-step plan with skip markers and command preview."""
    typer.echo("")
    typer.echo("Bootstrap plan:")
    for idx, step in enumerate(steps, start=1):
        if step.skipped:
            typer.echo(f"  {idx}. [skip] {step.label} — {step.skip_reason}")
            continue
        if step.cmd is None:
            typer.echo(f"  {idx}. {step.label}")
            if step.label.startswith("CREATE EXTENSION"):
                # Pure-Python equivalent of ``psql -c "CREATE EXTENSION
                # ..."`` — surface the SQL so the plan stays scrutable
                # even though there's no literal shell command behind it.
                typer.echo(
                    "       psycopg: CREATE EXTENSION IF NOT EXISTS vector"
                )
        else:
            typer.echo(f"  {idx}. {step.label}")
            typer.echo(f"       $ {' '.join(step.cmd)}")
    typer.echo("")
    if dry_run:
        typer.echo(
            "Dry-run mode (default). Re-run with --yes to execute the "
            "non-skip steps above."
        )


def _run_cmd(cmd: list[str]) -> None:
    """Run ``cmd``, streaming stdout/stderr, raising on non-zero exit."""
    typer.echo(f"  $ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)  # noqa: S603 — explicit args
    except FileNotFoundError as exc:
        raise typer.Exit(code=2) from exc
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(code=exc.returncode) from exc


def _create_vector_extension(db_name: str) -> None:
    """Run ``CREATE EXTENSION IF NOT EXISTS vector`` on ``db_name``.

    Routes the DDL through psycopg + the ``PgVectorExtensionMissing``
    wrapper from ``pg_migrations`` so the friendly install hint fires
    on every first-run path — not just the ``apply_migrations`` step.
    The previous implementation shelled out to ``psql -c`` here, which
    surfaced the bare server-side ``feature_not_supported`` error and
    bypassed the #1750 wrapper entirely (#1889).
    """
    from pollypm.storage.pg_migrations import PgVectorExtensionMissing

    import psycopg

    dsn = f"postgresql://localhost:5432/{db_name}"
    typer.echo(f"  $ psycopg.execute(\"CREATE EXTENSION IF NOT EXISTS vector\") @ {dsn}")
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — re-raise as friendly
        raise PgVectorExtensionMissing(
            "Could not install the `vector` extension on this Postgres "
            "instance. PollyPM requires pgvector for semantic recall "
            "and embeddings (see issue #1737).\n\n"
            "Fix (macOS/Homebrew):\n"
            "  brew install pgvector\n"
            "  brew services restart postgresql@17\n"
            "  # then re-run `pm bootstrap-pg --yes`\n\n"
            "Other platforms: https://github.com/pgvector/pgvector "
            "#installation-notes\n\n"
            f"Underlying error: {exc}"
        ) from exc


def _write_storage_block(config_path: Path, dsn: str) -> bool:
    """Write or append a ``[storage]`` block to ``config_path``.

    Returns True if the block was written (either as new file or
    appended to an existing one), False if the block was already
    present. Idempotent: a second run on a configured file is a no-op.

    On a brand-new install ``~/.pollypm/pollypm.toml`` does not yet
    exist (``pm bootstrap-pg`` is meant to be the FIRST command an
    operator runs). The previous implementation bailed in that case
    and left the runtime without a DSN; we now scaffold a minimal
    config containing just the ``[storage]`` block (#1888). ``pm
    example-config`` can still backfill the richer defaults later.
    """
    snippet = (
        "[storage]\n"
        f'url = "{dsn}"\n'
        "# Postgres is the required backend (sqlite was removed in "
        "the #1737 cutover).\n"
    )
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# pollypm.toml — created by `pm bootstrap-pg` on first "
            "run.\n"
            "# Run `pm example-config --force` later to backfill the "
            "full default config.\n\n"
        )
        config_path.write_text(header + snippet, encoding="utf-8")
        return True
    current = config_path.read_text(encoding="utf-8")
    if "\n[storage]\n" in current or current.startswith("[storage]\n"):
        return False
    config_path.write_text(
        current.rstrip() + "\n\n" + snippet, encoding="utf-8"
    )
    return True


_BOOTSTRAP_HELP = help_with_examples(
    (
        "Guided one-shot Postgres install for first-run operators.\n\n"
        "DEFAULT IS DRY-RUN. Re-run with --yes to actually mutate "
        "your system (brew install, services start, createdb, "
        "CREATE EXTENSION, schema apply, config write). Every "
        "destructive step is printed before it runs."
    ),
    [
        ("pm bootstrap-pg", "preview the plan (dry-run, no system changes)"),
        (
            "pm bootstrap-pg --yes",
            "execute the plan (brew + pgvector + createdb + schema + config)",
        ),
        (
            "pm bootstrap-pg --db pollypm-dev --yes",
            "bootstrap with a custom db name",
        ),
    ],
    trailing=(
        "macOS Homebrew is the fully-orchestrated path. On Linux / "
        "other platforms the brew steps are skipped with a hint; "
        "the database / extension / schema / config steps still run."
    ),
)


def bootstrap_pg(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Confirm the destructive steps. Without --yes the command "
            "runs in dry-run mode and prints the plan only."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Force dry-run mode even when --yes is set. Dry-run is the "
            "default; this flag is kept for clarity in scripts."
        ),
    ),
    pg_version: str = typer.Option(
        _DEFAULT_PG_VERSION,
        "--pg-version",
        help=(
            "Homebrew formula for Postgres. Default: postgresql@17 "
            "(the only version PollyPM is tested against)."
        ),
    ),
    db_name: str = typer.Option(
        _DEFAULT_DB_NAME,
        "--db",
        help="Database name to create. Default: pollypm.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        help="PollyPM config path to receive the [storage] block.",
    ),
) -> None:
    """``pm bootstrap-pg`` entry point (issue #1747)."""
    do_apply = yes and not dry_run

    typer.echo(f"PollyPM bootstrap-pg ({'apply' if do_apply else 'dry-run'})")
    typer.echo(f"  platform: {platform.system()} {platform.release()}")
    typer.echo(f"  pg formula: {pg_version}")
    typer.echo(f"  db name: {db_name}")
    typer.echo(f"  config: {config_path}")

    steps = _plan_steps(pg_version=pg_version, db_name=db_name)
    _print_plan(steps, dry_run=not do_apply)

    if not do_apply:
        # Dry-run exits cleanly so scripts can chain `pm bootstrap-pg
        # && pm bootstrap-pg --yes` for a preview-then-commit pattern.
        raise typer.Exit(code=0)

    # Confirmation gate. We already require --yes, but echo a final
    # warning so operators who alias `pm bootstrap-pg -y` still see it.
    typer.echo(
        "Executing plan. Each non-skip step prints its command before "
        "it runs; ctrl-c aborts cleanly between steps."
    )
    typer.echo("")

    for idx, step in enumerate(steps, start=1):
        typer.echo(f"[{idx}/{len(steps)}] {step.label}")
        if step.skipped:
            typer.echo(f"  skip — {step.skip_reason}")
            continue

        if step.cmd is not None:
            _run_cmd(step.cmd)
            continue

        # Pure-Python steps. Labels are stable keys for now.
        if step.label.startswith("CREATE EXTENSION"):
            from pollypm.storage.pg_migrations import (
                PgVectorExtensionMissing,
            )

            try:
                _create_vector_extension(db_name)
            except PgVectorExtensionMissing as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=3) from exc
            typer.echo("  extension `vector` ready")
            continue

        if step.label.startswith("apply_migrations"):
            from pollypm.storage import pg_migrations, pg_pool

            # The bootstrap command does not load a config (it's a
            # first-run tool that may run before pollypm.toml exists),
            # so we point the pool at the freshly-created localhost db
            # via POLLYPM_PG_DSN — pg_pool.resolve_dsn() picks the env
            # var ahead of any config / default.
            import os

            os.environ["POLLYPM_PG_DSN"] = (
                f"postgresql://localhost:5432/{db_name}"
            )
            pool = pg_pool.get_rw_pool(None)
            try:
                result = pg_migrations.apply_migrations(pool)
            except pg_migrations.PgVectorExtensionMissing as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=3) from exc
            if result.applied:
                applied = ", ".join(
                    f"{v}:{label}" for v, label in result.applied
                )
                typer.echo(f"  applied: {applied}")
            else:
                typer.echo("  schema already up to date")
            continue

        if step.label.startswith("Write [storage]"):
            wrote = _write_storage_block(
                config_path,
                dsn=f"postgresql://localhost:5432/{db_name}",
            )
            if wrote:
                typer.echo(f"  wrote [storage] block to {config_path}")
            else:
                typer.echo(
                    f"  [storage] block already present in {config_path}; "
                    "no edit"
                )
            continue

    typer.echo("")
    typer.echo("Bootstrap complete. Next steps:")
    typer.echo("  pm doctor-pg-connection   # confirm pg + pgvector are healthy")
    typer.echo("  pm up                     # relaunch the cockpit")
    raise typer.Exit(code=0)


storage_app = typer.Typer(
    help=help_with_examples(
        "Storage backend tooling (migration + ~/.pollypm/ disk usage).",
        [
            (
                "pm storage report",
                "show ~/.pollypm/ disk usage by subdir (bytes, files, mtime)",
            ),
            (
                "pm storage prune snapshots --older-than 14d --dry-run",
                "preview snapshot pruning without deleting anything",
            ),
            (
                "pm storage migrate-to-pg --commit --yes",
                "move data from sqlite to postgres and rename source files",
            ),
        ],
    ),
    no_args_is_help=True,
)


_MIGRATE_HELP = help_with_examples(
    (
        "Migrate the sqlite workspace state (and any per-project legacy "
        "DBs) into the Postgres backend installed by `pm doctor-pg-"
        "connection`.\n\n"
        "DEFAULT IS DRY-RUN. Pass --commit to actually mutate pg. The "
        "tool is idempotent: a re-run against an unchanged sqlite "
        "file is a no-op."
    ),
    [
        ("pm storage migrate-to-pg", "preview what would happen"),
        (
            "pm storage migrate-to-pg --commit --yes",
            "do the migration and rename source files",
        ),
        (
            "pm storage migrate-to-pg --from-sqlite ~/old.db --commit",
            "migrate a specific sqlite file (skips auto-discovery)",
        ),
    ],
    trailing=(
        "On --commit success each source state.db is renamed to "
        "state.db.pre-pg-<timestamp>. To roll back, flip the "
        "storage backend back to sqlite and rename the snapshot."
    ),
)


def register_storage_commands(app: typer.Typer) -> None:
    """Attach the ``storage`` sub-app + ``bootstrap-pg`` to the root CLI."""
    app.add_typer(storage_app, name="storage")
    # ``pm bootstrap-pg`` is a top-level command (not nested under
    # ``pm storage``) because it's the first command a brand-new user
    # runs — surfacing it at the root keeps the install path short
    # (issue #1747).
    app.command("bootstrap-pg", help=_BOOTSTRAP_HELP)(bootstrap_pg)


@storage_app.command("migrate-to-pg", help=_MIGRATE_HELP)
def migrate_to_pg(
    dsn: str = typer.Option(
        "",
        "--dsn",
        help=(
            "Override the Postgres DSN. Default: resolve from "
            "[storage] url / POLLYPM_PG_DSN env / built-in default."
        ),
    ),
    from_sqlite: str = typer.Option(
        "auto",
        "--from-sqlite",
        help=(
            "Sqlite source. 'auto' = workspace state.db + every "
            "per-project state.db registered in pollypm.toml. Pass a "
            "file path to migrate exactly that DB and skip discovery."
        ),
    ),
    include_legacy_per_project: bool = typer.Option(
        True,
        "--include-legacy-per-project/--no-legacy-per-project",
        help=(
            "Include per-project <project>/.pollypm/state.db files in "
            "auto-discovery. Off only for testing."
        ),
    ),
    reembed: bool = typer.Option(
        False,
        "--reembed",
        help=(
            "After copy, re-run `pm memory backfill-embeddings` so the "
            "new pgvector embeddings column is populated. Slice E "
            "wires the flag; the actual re-embed pass lands once "
            "Slice D's embedding writer is in (TODO)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Force dry-run mode even when --commit is set. The default "
            "is already a dry-run; this flag is kept for clarity in "
            "scripts."
        ),
    ),
    commit: bool = typer.Option(
        False,
        "--commit",
        help=(
            "Kill switch — only mutates pg when set. Without --commit "
            "the tool runs the entire pipeline against a rolled-back "
            "transaction so pre-flight + parity checks surface."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt before --commit.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """``pm storage migrate-to-pg`` entry point."""
    # The two flags can disagree: --dry-run overrides --commit so a
    # script that always passes --commit can opt back into preview mode
    # with --dry-run for a CI smoke pass.
    do_commit = commit and not dry_run

    # Load config — needed for auto-discovery + default DSN resolution.
    config = None
    resolved_config = resolve_config_path(config_path)
    if resolved_config.exists():
        try:
            config = load_config(resolved_config)
        except Exception as exc:  # noqa: BLE001
            typer.echo(
                f"Could not load config from {resolved_config}: {exc}",
                err=True,
            )
            raise typer.Exit(code=2) from exc
    # Note: config missing is OK if --from-sqlite is a specific path
    # AND --dsn is set. The pool factory will fall through to the env
    # var / default DSN; discovery will see exactly the one source.

    # Apply DSN override before opening the pool.
    if dsn:
        import os

        os.environ["POLLYPM_PG_DSN"] = dsn

    # Lazy import — these pull psycopg, which we don't want at module
    # import time so sqlite-only installs aren't paying the cost.
    from pollypm.storage import pg_migration_tool, pg_pool

    sources = pg_migration_tool.discover_sources(
        config=config,
        include_legacy_per_project=include_legacy_per_project,
        from_sqlite_override=from_sqlite,
    )

    if not sources:
        typer.echo(
            "No sqlite sources discovered. Either pass --from-sqlite "
            "<path> or check that workspace_root + projects in "
            "pollypm.toml point at directories containing .pollypm/state.db."
        )
        raise typer.Exit(code=0)

    typer.echo(f"Discovered {len(sources)} sqlite source(s):")
    for src in sources:
        suffix = (
            f" [project_key={src.project_key}]"
            if src.project_key
            else ""
        )
        typer.echo(f"  - {src.path} ({src.kind}){suffix}")
    typer.echo("")

    # Open the pool + run preflight before doing any per-source work.
    try:
        pool = pg_pool.get_rw_pool(config)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Could not open pg pool: {exc}\n"
            "Fix: confirm Postgres is running and POLLYPM_PG_DSN / "
            "[storage] url points at a reachable instance, then re-run.",
            err=True,
        )
        raise typer.Exit(code=3) from exc

    preflight = pg_migration_tool.preflight(pool)
    if not preflight.ok:
        typer.echo(f"Pre-flight failed: {preflight.message}", err=True)
        typer.echo(
            "Fix: run `pm doctor-pg-connection` to diagnose, ensure pg "
            ">= 16 is reachable, vector extension is installed, and "
            "open a PgWorkService once so schema migrations apply.",
            err=True,
        )
        raise typer.Exit(code=3)
    typer.echo(f"Pre-flight: {preflight.message}")
    typer.echo("")

    # Confirmation gate. We ONLY prompt when we're about to commit and
    # the user didn't pre-confirm with --yes. Dry-run never prompts.
    if do_commit and not yes:
        confirm = typer.confirm(
            f"About to copy {len(sources)} sqlite source(s) into pg and "
            "rename each state.db to state.db.pre-pg-<ts>. Continue?"
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    run = pg_migration_tool.migrate_sources(
        sources,
        pool=pool,
        commit=do_commit,
    )

    summary = pg_migration_tool.format_run_summary(run)
    typer.echo(summary)

    if reembed and do_commit and run.succeeded:
        # TODO(slice-D): wire `pm memory backfill-embeddings` once the
        # embedding writer (Slice D) is in. For now, surface a clear
        # follow-up rather than silently ignoring the flag.
        typer.echo("")
        typer.echo(
            "--reembed: deferred — the embedding writer lands in Slice "
            "D. Re-run `pm memory backfill-embeddings` manually once "
            "that PR ships."
        )

    if run.preflight_error:
        raise typer.Exit(code=3)
    if not run.succeeded:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


# --------------------------------------------------------------------- #
# ``pm storage report`` + ``pm storage prune`` — disk-bloat visibility. #
# --------------------------------------------------------------------- #
#
# Why this exists
# ---------------
#
# PR #2037 caught ``~/.pollypm/snapshots/`` silently growing past 1.4M
# files (~38 GB) — large enough to wedge the autouse pytest snapshot
# fixture for minutes per test. There was no surface that would have
# told the operator *which* subtree under ``~/.pollypm/`` was the one
# growing unbounded; ``du -sh ~/.pollypm/*`` walks every inode and
# itself takes long enough on snapshots/ that nobody runs it
# preventatively.
#
# ``pm storage report`` is that surface. It scans every top-level
# subdir of ``~/.pollypm/`` and prints a tabular summary: file count,
# byte total, oldest/newest mtime, and a NOTES column that flags
# unbounded growth / orphaned worktrees / etc. ``pm storage prune``
# is the matching cleanup: targeted, age-gated, dry-run by default,
# requires ``--yes`` to actually delete (no interactive prompt so the
# command works via tmux send-keys).
#
# Scan strategy
# -------------
#
# ``os.scandir`` is used everywhere — ``Path.rglob`` is ~4x slower at
# this volume because every iteration round-trips through ``PosixPath``
# construction. Two dir classes mirror the split that PR #2037 added
# to ``tests/conftest.py::_snapshot_guarded_dirs``:
#
# - ``_SHALLOW_ONLY_DIRS`` (e.g. ``snapshots/``) — recurse but stop
#   when we exceed ``_SCAN_FILE_CAP``; flag NOTES with the cap-hit
#   warning. These dirs are known to grow unbounded; the report
#   should still load fast even when they're already broken.
# - All other dirs — recursive, no cap. Even ``transcripts/`` at
#   100k files completes in well under a second with ``os.scandir``.
#
# The cap protects ``pm storage report`` against the exact failure
# mode it's meant to surface — a developer machine where
# ``snapshots/`` already has 1.4M files shouldn't have to wait 30s
# for the report that tells them that.


# Scanner internals (canonical subdir list, DirScan/HomeReport types,
# ``scan_pollypm_home`` and the walk helpers it depends on) live in
# the neutral :mod:`pollypm.storage_report` module so both this CLI
# surface AND ``GET /api/v1/storage`` consume the same source of
# truth without coupling the web route to the Typer/migration/prune
# CLI surface (Codex review on PR #2054 — module boundary fix).
#
# We re-export under the original ``_``-prefixed names because the
# prune paths below (and a handful of tests) still import them as
# ``pollypm.cli_features.storage._HOME_SUBDIRS`` etc.
from pollypm.storage_report import (  # noqa: E402
    HOME_SUBDIRS as _HOME_SUBDIRS,
    ORPHAN_WORKTREE_STALE_DAYS as _ORPHAN_WORKTREE_STALE_DAYS,
    SCAN_FILE_CAP as _SCAN_FILE_CAP,
    SHALLOW_ONLY_DIRS as _SHALLOW_ONLY_DIRS,
    DirScan,
    HomeReport,
    count_live_agent_worktrees as _count_live_agent_worktrees,
    count_orphan_worktrees as _count_orphan_worktrees,
    has_gz_descendant as _has_gz_descendant,
    iter_orphan_worktree_paths as _iter_orphan_worktree_paths,
    scan_config_files as _scan_config_files,
    scan_pollypm_home,
    scan_subdir as _scan_subdir,
)


def _format_bytes(n: int) -> str:
    """Render ``n`` bytes as the largest unit that keeps the integer < 1024.

    Output uses GB/MB/KB (not GiB/MiB) because the report is for
    operator scanning, not for binary precision. Negative inputs
    (shouldn't happen — defensive) round-trip as ``"0 B"``.
    """
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _format_mtime(ts: float | None) -> str:
    """Render ``ts`` (epoch seconds) as ``YYYY-MM-DD``; dash when None."""
    if ts is None or ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")



def _sort_rows(rows: list[DirScan], *, key: str) -> list[DirScan]:
    """Stable-sort rows by the requested key (descending).

    ``files`` and ``bytes`` are obvious; ``mtime`` uses the newer of
    oldest/newest (newest_mtime), with None sorted last. Unknown
    keys raise — the Typer surface validates before calling us.
    """
    if key == "files":
        return sorted(rows, key=lambda r: r.files, reverse=True)
    if key == "bytes":
        return sorted(rows, key=lambda r: r.bytes, reverse=True)
    if key == "mtime":
        return sorted(
            rows,
            key=lambda r: r.newest_mtime if r.newest_mtime is not None else 0.0,
            reverse=True,
        )
    raise ValueError(f"unknown sort key: {key!r}")


def _render_report_text(report: HomeReport, *, sort_key: str) -> str:
    """Render the human-facing ``pm storage report`` table.

    The header line mirrors the spec's preamble (``total: <bytes>,
    <files> files``). Columns: DIR, FILES, BYTES, OLDEST, NEWEST,
    NOTES. Widths are dynamic so a very long NOTES doesn't break
    the table — we right-pad fixed-width columns and leave NOTES
    free-form at the end.
    """
    lines: list[str] = []
    total_bytes = report.total_bytes
    total_files = report.total_files
    lines.append(
        f"{report.home}/  total: {_format_bytes(total_bytes)}, "
        f"{total_files:,} files"
    )
    lines.append("")

    rows = _sort_rows(list(report.rows), key=sort_key)

    header = ("DIR", "FILES", "BYTES", "OLDEST", "NEWEST", "NOTES")
    formatted: list[tuple[str, ...]] = [header]
    for row in rows:
        if row.files == 0 and row.bytes == 0 and not row.note:
            # Skip absent / empty subdirs — keeps the report short
            # on fresh installs where most subdirs don't exist yet.
            continue
        files_str = f"{row.files:,}" + ("+" if row.cap_hit else "")
        formatted.append(
            (
                f"{row.name}/",
                files_str,
                _format_bytes(row.bytes),
                _format_mtime(row.oldest_mtime),
                _format_mtime(row.newest_mtime),
                row.note,
            )
        )
    if report.config_files > 0:
        formatted.append(
            (
                "config files",
                f"{report.config_files:,}",
                _format_bytes(report.config_bytes),
                "-",
                _format_mtime(report.config_newest_mtime),
                "",
            )
        )

    # Compute per-column widths from the rendered cells. Last
    # column (NOTES) stays free-form — no padding.
    widths = [0] * len(header)
    for row in formatted:
        for i, cell in enumerate(row):
            if i == len(header) - 1:
                continue
            widths[i] = max(widths[i], len(cell))

    for i, row in enumerate(formatted):
        parts = []
        for j, cell in enumerate(row):
            if j == len(header) - 1:
                parts.append(cell)
            else:
                parts.append(cell.ljust(widths[j]))
        lines.append("  ".join(parts).rstrip())
        if i == 0:
            # Separator under header.
            sep_parts = []
            for j in range(len(header)):
                if j == len(header) - 1:
                    sep_parts.append("-----")
                else:
                    sep_parts.append("-" * widths[j])
            lines.append("  ".join(sep_parts).rstrip())

    return "\n".join(lines) + "\n"


def _render_report_json(report: HomeReport, *, sort_key: str) -> str:
    """Render the report as a JSON document.

    Schema:

    ```
    {
      "home": "/home/user/.pollypm",
      "total_files": 1498221,
      "total_bytes": 44230000000,
      "subdirs": [
        {"name": "snapshots", "files": 1395189, "bytes": ...,
         "oldest_mtime": "...", "newest_mtime": "...",
         "cap_hit": true, "note": "..."},
        ...
      ],
      "config_files": {"files": 12, "bytes": 122880,
                       "newest_mtime": "..."}
    }
    ```

    ``mtime`` fields are emitted as ISO-8601 UTC for portability;
    epoch ints stay machine-friendly but ISO is what every caller
    actually wants when they pipe this into jq.
    """
    def _iso(ts: float | None) -> str | None:
        if ts is None or ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    rows = _sort_rows(list(report.rows), key=sort_key)
    payload = {
        "home": str(report.home),
        "total_files": report.total_files,
        "total_bytes": report.total_bytes,
        "subdirs": [
            {
                "name": row.name,
                "files": row.files,
                "bytes": row.bytes,
                "oldest_mtime": _iso(row.oldest_mtime),
                "newest_mtime": _iso(row.newest_mtime),
                "cap_hit": row.cap_hit,
                "note": row.note,
            }
            for row in rows
        ],
        "config_files": {
            "files": report.config_files,
            "bytes": report.config_bytes,
            "newest_mtime": _iso(report.config_newest_mtime),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


_OLDER_THAN_RE = re.compile(r"^\s*(\d+)\s*([dwhm])\s*$", re.IGNORECASE)


def _parse_older_than(spec: str) -> float:
    """Parse a ``--older-than`` spec (``7d``, ``4w``, ``24h``, ``30m``).

    Returns the threshold in seconds. Raises ``typer.BadParameter`` on
    malformed input; the caller propagates that so the Typer error
    surface stays consistent with the rest of the CLI.
    """
    match = _OLDER_THAN_RE.match(spec or "")
    if not match:
        raise typer.BadParameter(
            f"Could not parse --older-than={spec!r}. "
            "Expected a number with a unit suffix: "
            "7d (days), 4w (weeks), 24h (hours), 30m (minutes)."
        )
    n = int(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}[unit]
    if n <= 0:
        raise typer.BadParameter(
            f"--older-than must be positive (got {spec!r})."
        )
    return n * multiplier


@dataclass(slots=True)
class _PruneCandidate:
    """One filesystem entry the pruner has selected for deletion.

    ``path`` is the absolute path; ``bytes`` is the recursive size
    (so dry-run totals are accurate); ``is_dir`` chooses between
    ``unlink`` and ``rmtree`` on commit.
    """

    path: Path
    bytes: int
    is_dir: bool


def _dir_size(path: Path) -> int:
    """Recursively sum file sizes under ``path``. Best-effort.

    Used to populate ``_PruneCandidate.bytes`` so dry-run totals
    match what the actual ``rmtree`` will reclaim. ``OSError`` is
    swallowed per-entry — a dir with unreadable children still
    reports a (lower-bound) size.
    """
    total = 0
    stack: list[str] = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _iter_prune_candidates_snapshots(
    home: Path, *, older_than_seconds: float
) -> Iterable[_PruneCandidate]:
    """Yield snapshot files older than the threshold.

    Snapshots are flat-file artifacts (one file per snapshot), so
    we walk via scandir and emit per-file candidates. mtime is the
    snapshot's own write time — exactly what we want for "older
    than 14 days".
    """
    root = home / "snapshots"
    if not root.is_dir():
        return
    cutoff = time.time() - older_than_seconds
    stack: list[str] = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime <= cutoff:
                        yield _PruneCandidate(
                            path=Path(entry.path),
                            bytes=st.st_size,
                            is_dir=False,
                        )
        except OSError:
            continue


def _iter_prune_candidates_transcripts(
    home: Path, *, older_than_seconds: float
) -> Iterable[_PruneCandidate]:
    """Yield transcript files older than the threshold.

    Same shape as snapshots — flat-file scan. Transcripts are
    append-only artifacts that live in per-session subdirs; we
    walk recursively and emit per-file candidates so a recent
    session keeps its in-flight transcript even when older ones
    in the same dir get pruned.
    """
    root = home / "transcripts"
    if not root.is_dir():
        return
    cutoff = time.time() - older_than_seconds
    stack: list[str] = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime <= cutoff:
                        yield _PruneCandidate(
                            path=Path(entry.path),
                            bytes=st.st_size,
                            is_dir=False,
                        )
        except OSError:
            continue


class _LiveSetUnknownError(RuntimeError):
    """Raised when prune is asked to enumerate worktree/home candidates
    but ``_count_live_agent_worktrees`` returned ``None`` (live-agent
    detection failed).

    The CLI surface (``storage_prune``) catches this and exits non-zero
    with a clear "refusing to prune" message. The point is to never let
    "I don't know what's live" silently degrade to "nothing is live" —
    the latter would treat every stale ``agent-*`` dir as orphaned and
    delete LIVE agents' work on a machine whose work service is briefly
    unreachable (Codex PR #2040 blocker).
    """


def _iter_prune_candidates_worktrees(
    home: Path, *, older_than_seconds: float
) -> Iterable[_PruneCandidate]:
    """Yield orphan worktree DIRS older than the threshold.

    Worktrees are pruned at the dir level (not per file) because
    a worktree is the atomic unit — half-deleting one is worse
    than leaving it. We layer two safety checks:

    1. The dir's basename is NOT in the live-agent set (orphan-only).
    2. The dir's mtime is older than ``older_than_seconds`` (typical
       call site: ``--older-than 1d`` matches the standard orphan
       definition, but the operator can pass ``--older-than 7d``
       for a more conservative sweep).

    REFUSES TO ENUMERATE (raises ``_LiveSetUnknownError``) when
    ``_count_live_agent_worktrees`` returns ``None`` — see that
    function's docstring for the safety contract. An empty *known*
    live set is fine (the work service is reachable, just no active
    agents); an *unknown* live set must not be silently treated the
    same way.
    """
    live_names = _count_live_agent_worktrees(home)
    if live_names is None:
        raise _LiveSetUnknownError(
            "live-agent detection failed — refusing to prune worktrees"
        )
    for path in _iter_orphan_worktree_paths(
        home,
        live_names=live_names,
        stale_after_seconds=older_than_seconds,
    ):
        yield _PruneCandidate(
            path=path,
            bytes=_dir_size(path),
            is_dir=True,
        )


def _iter_prune_candidates_homes(
    home: Path, *, older_than_seconds: float
) -> Iterable[_PruneCandidate]:
    """Yield agent home subdirs whose agent task has completed AND mtime > threshold.

    Same orphan-detection plumbing as worktrees: a ``homes/agent-<id>``
    dir is prune-eligible only if no in-progress task references it
    AND it has been idle longer than ``older_than_seconds``.

    REFUSES TO ENUMERATE (raises ``_LiveSetUnknownError``) when the
    live-agent set is unknown (work service unreachable, config
    missing, listing raised). This matches the docstring's original
    promise that "an unreachable service gets nothing pruned" — the
    previous implementation contradicted that by returning ``set()``
    on failure, which the orphan filter then treated as "no agents
    are live" and queued every ``homes/agent-*`` dir for deletion.
    """
    live_names = _count_live_agent_worktrees(home)
    if live_names is None:
        raise _LiveSetUnknownError(
            "live-agent detection failed — refusing to prune homes"
        )
    # The homes dir name varies: ``homes/`` (current) and ``agent_homes/``
    # (legacy alias used by some plugins). Prune the modern one only —
    # the legacy alias would conflate with operator state if cleaned
    # without explicit operator intent.
    root = home / "homes"
    if not root.is_dir():
        return
    cutoff = time.time() - older_than_seconds
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name in live_names:
                    continue
                if st.st_mtime > cutoff:
                    continue
                yield _PruneCandidate(
                    path=Path(entry.path),
                    bytes=_dir_size(Path(entry.path)),
                    is_dir=True,
                )
    except OSError:
        return


_PRUNE_TARGETS: dict[str, callable] = {  # type: ignore[type-arg]
    "snapshots": _iter_prune_candidates_snapshots,
    "transcripts": _iter_prune_candidates_transcripts,
    "worktrees": _iter_prune_candidates_worktrees,
    "homes": _iter_prune_candidates_homes,
}


_REPORT_HELP = help_with_examples(
    (
        "Show disk usage of ``~/.pollypm/`` broken down by subdir.\n\n"
        "Surfaces the silent-growth subtrees that ``du -sh`` is too "
        "slow to enumerate. Flags unbounded growth, orphan worktrees, "
        "and audit rotation in the NOTES column."
    ),
    [
        ("pm storage report", "tabular report sorted by bytes (default)"),
        (
            "pm storage report --sort=files",
            "re-sort by file count instead of bytes",
        ),
        ("pm storage report --json", "machine-readable output for scripts"),
    ],
    trailing=(
        "Scans use os.scandir for speed. ``snapshots/`` is "
        "capped at 50k files to keep the report fast on already-"
        "broken installs; the cap-hit shows as ``50,000+`` and "
        "flags ``unbounded growth`` in NOTES."
    ),
)


@storage_app.command("report", help=_REPORT_HELP)
def storage_report(
    sort: str = typer.Option(
        "bytes",
        "--sort",
        help="Sort by bytes (default), files, or mtime.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of the human-readable table.",
    ),
    home: Path = typer.Option(
        None,
        "--home",
        help=(
            "Override the ``~/.pollypm/`` location. Mostly for tests; "
            "production should use the default."
        ),
    ),
) -> None:
    """``pm storage report`` entry point — see module docstring for design."""
    if sort not in ("bytes", "files", "mtime"):
        raise typer.BadParameter(
            f"--sort must be one of: bytes, files, mtime (got {sort!r})."
        )
    target = home if home is not None else Path.home() / ".pollypm"
    if not target.is_dir():
        typer.echo(
            f"{target}/ does not exist yet. Run `pm up` once to "
            "create the workspace, then re-run `pm storage report`."
        )
        raise typer.Exit(code=0)

    report = scan_pollypm_home(target)
    if json_output:
        typer.echo(_render_report_json(report, sort_key=sort), nl=False)
    else:
        typer.echo(_render_report_text(report, sort_key=sort), nl=False)
    raise typer.Exit(code=0)


_PRUNE_HELP = help_with_examples(
    (
        "Delete old artifacts under ``~/.pollypm/<target>/``.\n\n"
        "DEFAULT IS DRY-RUN. ``--yes`` actually deletes (no interactive "
        "prompt — works under tmux send-keys). Targets: snapshots, "
        "transcripts, worktrees (orphans only), homes (completed-agent only)."
    ),
    [
        (
            "pm storage prune snapshots --older-than 14d --dry-run",
            "preview the prune (no deletion)",
        ),
        (
            "pm storage prune snapshots --older-than 14d --yes",
            "delete snapshots older than 14 days",
        ),
        (
            "pm storage prune worktrees --older-than 7d --yes",
            "delete orphan worktrees stale > 7 days",
        ),
    ],
    trailing=(
        "Safety: prune NEVER touches files newer than --older-than, "
        "NEVER touches worktrees / homes whose agent is still in-"
        "progress, and ALWAYS requires --yes (or --dry-run). "
        "Without either flag the command refuses to run."
    ),
)


@storage_app.command("prune", help=_PRUNE_HELP)
def storage_prune(
    target: str = typer.Argument(
        ...,
        help="Subdir to prune: snapshots, transcripts, worktrees, homes.",
    ),
    older_than: str = typer.Option(
        ...,
        "--older-than",
        help="Age threshold, e.g. 7d, 4w, 24h, 30m. Required.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted; never modify the filesystem.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Confirm destructive deletion. Required (alongside the absence "
            "of --dry-run) to actually delete. There is no interactive "
            "prompt so the command works via tmux send-keys."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of the human-readable summary.",
    ),
    sample: int = typer.Option(
        5,
        "--sample",
        help="Number of sample paths to show in the dry-run summary.",
    ),
    home: Path = typer.Option(
        None,
        "--home",
        help="Override ``~/.pollypm/`` (tests only).",
    ),
) -> None:
    """``pm storage prune`` entry point.

    Refuses to run when neither ``--dry-run`` nor ``--yes`` is set so
    a typo can't accidentally delete: the operator must opt in to
    EITHER a preview or a destructive action.
    """
    if target not in _PRUNE_TARGETS:
        raise typer.BadParameter(
            f"Unknown prune target {target!r}. "
            f"Valid: {', '.join(sorted(_PRUNE_TARGETS))}."
        )
    if not dry_run and not yes:
        typer.echo(
            "Refusing to run without --dry-run or --yes. "
            "Pass --dry-run to preview, or --yes to actually delete.",
            err=True,
        )
        raise typer.Exit(code=2)
    if dry_run and yes:
        # Not a fatal error — surface the conflict and prefer the
        # safer interpretation (dry-run wins).
        typer.echo(
            "Note: both --dry-run and --yes passed; --dry-run takes "
            "precedence (nothing will be deleted).",
            err=True,
        )

    try:
        older_than_seconds = _parse_older_than(older_than)
    except typer.BadParameter:
        raise

    home_path = home if home is not None else Path.home() / ".pollypm"
    if not home_path.is_dir():
        typer.echo(
            f"{home_path}/ does not exist. Nothing to prune.",
            err=True,
        )
        raise typer.Exit(code=0)

    iterator = _PRUNE_TARGETS[target]
    try:
        candidates: list[_PruneCandidate] = list(
            iterator(home_path, older_than_seconds=older_than_seconds)
        )
    except _LiveSetUnknownError as exc:
        # Live-agent detection failed (work service unreachable / config
        # missing / listing raised). REFUSE to prune rather than let the
        # caller silently treat an unknown live set as an empty one —
        # the latter would delete LIVE agent directories. Codex PR
        # #2040 blocker.
        typer.echo(
            f"{exc}.\n"
            "Fix: run `pm doctor` to diagnose the work service "
            "connection, confirm Postgres is reachable and "
            "pollypm.toml is loadable, then re-run.",
            err=True,
        )
        raise typer.Exit(code=3) from exc

    total_bytes = sum(c.bytes for c in candidates)
    total_count = len(candidates)
    sample_paths = [str(c.path) for c in candidates[: max(sample, 0)]]

    if json_output:
        payload = {
            "target": target,
            "older_than": older_than,
            "older_than_seconds": older_than_seconds,
            "dry_run": dry_run or not yes,
            "candidate_count": total_count,
            "candidate_bytes": total_bytes,
            "sample_paths": sample_paths,
            "deleted_count": 0,
            "deleted_bytes": 0,
        }
    else:
        typer.echo(
            f"Prune target: {target}/ (older than {older_than}) — "
            f"{total_count:,} candidate(s), {_format_bytes(total_bytes)}"
        )
        if sample_paths:
            typer.echo("Sample paths:")
            for p in sample_paths:
                typer.echo(f"  - {p}")
            if total_count > len(sample_paths):
                typer.echo(
                    f"  ... and {total_count - len(sample_paths):,} more"
                )

    if dry_run or not yes:
        if json_output:
            typer.echo(json.dumps(payload, indent=2) + "\n", nl=False)
        else:
            typer.echo("(dry-run — no files were deleted)")
        raise typer.Exit(code=0)

    # Destructive path. Each candidate is unlinked/rmtree'd
    # independently — a single bad path (vanished mid-scan, EACCES,
    # etc.) shouldn't abort the whole prune.
    deleted_count = 0
    deleted_bytes = 0
    failures: list[tuple[str, str]] = []
    for cand in candidates:
        try:
            if cand.is_dir:
                shutil.rmtree(cand.path)
            else:
                cand.path.unlink()
        except FileNotFoundError:
            # Already gone — count as success since the end state
            # matches what the operator asked for.
            deleted_count += 1
            deleted_bytes += cand.bytes
            continue
        except OSError as exc:
            failures.append((str(cand.path), str(exc)))
            continue
        deleted_count += 1
        deleted_bytes += cand.bytes

    if json_output:
        payload["deleted_count"] = deleted_count
        payload["deleted_bytes"] = deleted_bytes
        payload["dry_run"] = False
        payload["failures"] = [
            {"path": path, "error": msg} for path, msg in failures
        ]
        typer.echo(json.dumps(payload, indent=2) + "\n", nl=False)
    else:
        typer.echo(
            f"Deleted {deleted_count:,} of {total_count:,} candidate(s), "
            f"reclaimed {_format_bytes(deleted_bytes)}."
        )
        if failures:
            typer.echo(f"{len(failures)} failure(s):", err=True)
            for path, msg in failures[:5]:
                typer.echo(f"  - {path}: {msg}", err=True)
            if len(failures) > 5:
                typer.echo(
                    f"  ... and {len(failures) - 5:,} more", err=True
                )
    if failures:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)
