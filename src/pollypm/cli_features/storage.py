"""``pm storage`` CLI — Postgres migration tooling (issue #1737, Slice E).

Slice E ships the operator-facing ``pm storage migrate-to-pg`` command
that moves data from the existing sqlite workspace into the pg schema
installed by Slice A. The implementation lives in
:mod:`pollypm.storage.pg_migration_tool`; this module is the typer-level
glue: flag parsing, confirmation prompt, structured failure messages.

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

from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


__all__ = ["storage_app", "register_storage_commands"]


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
    """Attach the ``storage`` sub-app to the root CLI."""
    app.add_typer(storage_app, name="storage")


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
