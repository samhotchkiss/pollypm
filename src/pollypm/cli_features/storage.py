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

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


__all__ = [
    "bootstrap_pg",
    "register_storage_commands",
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
        "Storage backend tooling (sqlite ↔ postgres migration).",
        [
            (
                "pm storage migrate-to-pg",
                "preview the sqlite → postgres data migration (dry-run)",
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
