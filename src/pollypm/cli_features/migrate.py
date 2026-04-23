"""`pm migrate` — schema migration gate CLI (#717).

Contract:
- Inputs: ``--check`` for dry-run, ``--apply`` for real upgrades, plus
  the standard ``--config`` option so the workspace state.db resolves
  exactly like every other ``pm`` command.
- Outputs: human-readable progress lines on stdout, diagnostics on
  stderr, standard exit codes (0 on success, 2 when a pending migration
  would block, 3 on dry-run failure).
- Side effects (``--check``): copies the workspace state.db to
  ``~/.pollypm/migration-check.db`` and applies pending migrations
  against the clone.
- Side effects (``--apply``): opens the live state.db and runs pending
  migrations atomically per-migration.
- Invariants: never mutates the live DB during ``--check``. Sets
  ``POLLYPM_SKIP_MIGRATION_GATE`` before opening the store so the gate
  in ``pm up`` cannot loop on itself.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


_MIGRATE_HELP = help_with_examples(
    "Apply or dry-run pending schema migrations on the workspace state DB.",
    [
        ("pm migrate --check", "dry-run pending migrations against a clone"),
        ("pm migrate --apply", "apply pending migrations to the live DB"),
    ],
    trailing=(
        "The cockpit refuses to start when migrations are pending. "
        "Run --apply to clear the gate, or --check first to see what "
        "would change."
    ),
)


def _resolve_state_db(config_path: Path) -> Path:
    """Return the workspace-scope state.db path from the resolved config."""
    path = resolve_config_path(config_path)
    if not path.exists():
        raise typer.BadParameter(
            f"Config not found at {path}. Run `pm onboard` first."
        )
    config = load_config(path)
    return config.project.state_db


def register_migrate_commands(app: typer.Typer) -> None:
    @app.command("migrate", help=_MIGRATE_HELP)
    def migrate(
        check: bool = typer.Option(
            False, "--check", help="Dry-run pending migrations on a DB clone."
        ),
        apply: bool = typer.Option(
            False, "--apply", help="Apply pending migrations to the live DB."
        ),
        config_path: Path = typer.Option(
            DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
        ),
    ) -> None:
        if check == apply:
            raise typer.BadParameter(
                "Specify exactly one of --check or --apply."
            )

        db_path = _resolve_state_db(config_path)

        # Bypass the refuse-start gate — pm migrate is the designated
        # tool that fixes the situation the gate protects against, so
        # opening the store here must not trip the guard.
        from pollypm.store import migrations as _migrations
        _migrations.set_bypass(True)

        if check:
            _run_check(db_path)
        else:
            _run_apply(db_path)


def _run_check(db_path: Path) -> None:
    from pollypm.store import migrations as _migrations

    status = _migrations.inspect(db_path)
    if status.up_to_date:
        typer.echo(f"All migrations up to date ({db_path}).")
        raise typer.Exit(code=0)

    typer.echo(f"Dry-run against clone of {db_path}")
    typer.echo(_migrations.format_pending_summary(status))

    outcome = _migrations.check_against_clone(db_path)
    if not outcome.ok:
        # #760 — surface the failure in the canonical four-field shape so
        # users get a scannable summary + specific next action.
        from pollypm.structured_message import StructuredUserMessage

        msg = StructuredUserMessage(
            summary="Migration check FAILED — live DB untouched.",
            why=(
                "Replaying pending migrations against a clone of your "
                "state.db raised an error. Your real DB has not been "
                "modified, but the migrations can't be applied safely "
                "until this is resolved."
            ),
            next_action=(
                "Read the error below, fix the underlying cause, then "
                "re-run `pm migrate --check`."
            ),
            details=str(outcome.error or "").strip() or "No error detail reported.",
        )
        typer.echo(msg.render_cli(show_details=True), err=True)
        raise typer.Exit(code=3)

    if outcome.tables_changed:
        typer.echo("Schema changes on clone:")
        for change in outcome.tables_changed:
            typer.echo(f"  {change}")
    else:
        typer.echo("No table-level changes — migrations affect columns/indexes only.")
    if outcome.clone_path is not None:
        typer.echo(f"Clone retained at {outcome.clone_path} for inspection.")
    typer.echo("Migration check OK. Run `pm migrate --apply` to upgrade the live DB.")


def _run_apply(db_path: Path) -> None:
    from pollypm.store import migrations as _migrations

    outcome = _migrations.apply(db_path)
    if outcome.already_up_to_date:
        typer.echo("All migrations up to date.")
        return
    typer.echo(f"Applied {len(outcome.applied)} migration(s) to {db_path}:")
    for item in outcome.applied:
        typer.echo(f"  [{item.namespace}] v{item.version}: {item.description}")
