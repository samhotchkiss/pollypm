"""`pm migrate` — schema migration gate CLI (#717).

Contract:
- Inputs: ``--check`` for dry-run, ``--apply`` for real upgrades, plus
  the standard ``--config`` option so the workspace state.db resolves
  exactly like every other ``pm`` command.
- Outputs: human-readable progress lines on stdout, diagnostics on
  stderr, standard exit codes (0 on success, 2 when a pending migration
  would block, 3 on dry-run failure, 4 when ``--apply`` would race a
  live cockpit/rail_daemon — see #1006).
- Side effects (``--check``): copies the workspace state.db to
  ``~/.pollypm/migration-check.db`` and applies pending migrations
  against the clone.
- Side effects (``--apply``): opens the live state.db and runs pending
  migrations atomically per-migration. Refuses when ``rail_daemon`` is
  alive (#1006) — running migrations underneath a live JobWorkerPool
  closes per-project DB handles the pool is still using and triggers a
  ``Cannot operate on a closed database`` cascade that zombies the
  cockpit. Pass ``--force`` to override (only safe if you're certain no
  worker is mid-flight).
- Invariants: never mutates the live DB during ``--check``. Sets
  ``POLLYPM_SKIP_MIGRATION_GATE`` before opening the store so the gate
  in ``pm up`` cannot loop on itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path


# Exit code for "refused because something else is alive". Distinct from
# 2 (pending migration blocks) and 3 (dry-run failed) so scripts can tell
# the failure modes apart.
_EXIT_LIVE_PROCESS = 4


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
        force: bool = typer.Option(
            False,
            "--force",
            help=(
                "Skip the live-process safety check (rail_daemon detection). "
                "Only safe when no cockpit, rail_daemon, or job-worker is "
                "currently running — otherwise migration may close DB "
                "handles that running workers still hold (#1006)."
            ),
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
            _run_apply(db_path, force=force)


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


def _live_pollypm_processes() -> list[tuple[str, int, Path]]:
    """Return ``[(label, pid, pidfile)]`` for any live PollyPM process.

    Currently only inspects the rail_daemon PID file (the cockpit's
    own HeartbeatRail piggybacks on the same daemon when both are
    running, and has no separate PID file). The returned list is empty
    when nothing is running. Stale PID files (process gone) are
    filtered out so a clean machine never trips the guard.
    """
    home = Path(DEFAULT_CONFIG_PATH).parent
    candidates = (("rail_daemon", home / "rail_daemon.pid"),)
    live: list[tuple[str, int, Path]] = []
    for label, pidfile in candidates:
        if not pidfile.exists():
            continue
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid <= 0 or not _pid_alive(pid):
            continue
        live.append((label, pid, pidfile))
    return live


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` names a currently-running process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user owns the pid — treat as alive from our POV
        # rather than try to migrate over the top of someone else.
        return True
    return True


def _refuse_live_processes(live: list[tuple[str, int, Path]]) -> None:
    """Render a structured refusal and exit when live processes block --apply."""
    from pollypm.structured_message import StructuredUserMessage

    bullets = "\n".join(
        f"  - {label} (pid {pid}, pidfile {pidfile})"
        for label, pid, pidfile in live
    )
    msg = StructuredUserMessage(
        summary="Refusing to apply migrations — live PollyPM process detected.",
        why=(
            "Running migrations while the cockpit / rail_daemon / "
            "job-worker pool is alive can close per-project DB handles "
            "the pool is still holding, triggering a 'Cannot operate "
            "on a closed database' cascade that zombies the cockpit "
            "(#1006). Stop them first so the migration runs on a "
            "quiescent system."
        ),
        next_action=(
            "Stop the live processes, then re-run `pm migrate --apply`. "
            "If you are certain no worker is mid-flight, pass `--force` "
            "to override."
        ),
        details=f"Live processes:\n{bullets}",
    )
    typer.echo(msg.render_cli(show_details=True), err=True)
    raise typer.Exit(code=_EXIT_LIVE_PROCESS)


def _run_apply(db_path: Path, *, force: bool = False) -> None:
    from pollypm.store import migrations as _migrations

    if not force:
        live = _live_pollypm_processes()
        if live:
            _refuse_live_processes(live)

    outcome = _migrations.apply(db_path)
    if outcome.already_up_to_date:
        typer.echo("All migrations up to date.")
    else:
        migration_word = "migration" if len(outcome.applied) == 1 else "migrations"
        typer.echo(
            f"Applied {len(outcome.applied)} {migration_word} to {db_path}:"
        )
        for item in outcome.applied:
            typer.echo(f"  [{item.namespace}] v{item.version}: {item.description}")
    _run_legacy_per_project_db_migration()


def _run_legacy_per_project_db_migration() -> None:
    """Migrate any leftover per-project state.db files into workspace (#1004).

    Idempotent: each project's per-project DB is migrated and archived
    once. A re-run reports zero copied rows.
    """
    try:
        from pollypm.work.legacy_per_project_db import (
            migrate_legacy_per_project_dbs,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Per-project DB migration unavailable: {exc}", err=True)
        return

    reports = migrate_legacy_per_project_dbs()
    if not reports:
        return

    actionable = [r for r in reports if any(r.rows_copied.values()) or r.errors]
    if not actionable:
        return

    typer.echo("")
    typer.echo("Per-project state.db migration (#1004):")
    for report in actionable:
        if report.errors:
            typer.echo(
                f"  {report.project_key}: FAILED — {'; '.join(report.errors)}",
                err=True,
            )
            continue
        copied_summary = ", ".join(
            f"{table}={count}"
            for table, count in report.rows_copied.items()
            if count
        ) or "no rows"
        archive = (
            f" → archived {report.archived_to.name}"
            if report.archived_to is not None
            else ""
        )
        typer.echo(f"  {report.project_key}: {copied_summary}{archive}")
