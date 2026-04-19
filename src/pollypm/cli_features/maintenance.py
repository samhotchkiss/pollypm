"""Maintenance and account CLI commands.

Contract:
- Inputs: Typer options/arguments for doctoring, repair, upgrades,
  backups, restores, accounts, and reporting.
- Outputs: root command registrations on the passed Typer app.
- Side effects: account login mutations, filesystem repair, external
  install checks, and backup/restore operations.
- Invariants: long operational workflows stay out of ``pollypm.cli``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from pollypm.accounts import (
    add_account_via_login,
    list_account_statuses,
    probe_account_usage,
    relogin_account,
    remove_account as remove_account_entry,
)
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    GLOBAL_CONFIG_DIR,
    load_config,
)
from pollypm.doc_scaffold import repair_docs, verify_docs
from pollypm.errors import format_config_not_found_error
from pollypm.models import ProviderKind
from pollypm.service_api import PollyPMService
from pollypm.storage.state import StateStore
from pollypm.worktrees import list_worktrees as list_project_worktrees


def register_maintenance_commands(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        json_output: bool = typer.Option(
            False,
            "--json",
            help="Emit machine-readable JSON instead of the human checklist.",
        ),
        fix: bool = typer.Option(
            False,
            "--fix",
            help="Auto-fix the safe subset (missing dirs, stale panes).",
        ),
        fix_dry_run: bool = typer.Option(
            False,
            "--fix-dry-run",
            help="Show what --fix WOULD do, without mutating anything.",
        ),
    ) -> None:
        from pollypm.doctor import (
            apply_fixes,
            manual_fixes,
            planned_fixes,
            render_fix_dry_run,
            render_fix_summary,
            render_human,
            render_json,
            run_checks,
        )

        report = run_checks()
        if fix_dry_run:
            planned = planned_fixes(report)
            manual = manual_fixes(report)
            typer.echo(render_fix_dry_run(planned, manual))
            raise typer.Exit(code=0 if report.ok else 1)
        fix_summary: str | None = None
        if fix:
            fix_results = apply_fixes(report)
            if fix_results:
                for name, success, message in fix_results:
                    glyph = "fixed" if success else "fix failed"
                    typer.echo(f"  [{glyph}] {name}: {message}")
            manual_before_rerun = manual_fixes(report)
            fix_summary = render_fix_summary(fix_results, manual_before_rerun)
            if fix_results:
                report = run_checks()
        if json_output:
            typer.echo(render_json(report))
        else:
            typer.echo(render_human(report))
        if fix_summary:
            typer.echo("")
            typer.echo(fix_summary)
        raise typer.Exit(code=0 if report.ok else 1)

    @app.command()
    def accounts(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        for account in list_account_statuses(config_path):
            typer.echo(
                f"- {account.key}: {account.email} [{account.provider.value}] "
                f"logged_in={'yes' if account.logged_in else 'no'} "
                f"health={account.health} usage={account.usage_summary} "
                f"isolation={account.isolation_status}"
            )
            typer.echo(
                f"  isolation_summary={account.isolation_summary} "
                f"auth_storage={account.auth_storage} "
                f"profile_root={account.profile_root or '-'}"
            )
            if account.isolation_recommendation:
                typer.echo(
                    f"  isolation_recommendation="
                    f"{account.isolation_recommendation}"
                )
            if account.available_at or account.access_expires_at or account.reason:
                typer.echo(
                    f"  reason={account.reason or '-'} "
                    f"available_at={account.available_at or '-'} "
                    f"access_expires_at={account.access_expires_at or '-'}"
                )

    @app.command("account-doctor")
    def account_doctor(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        config = load_config(config_path)
        statuses = list_account_statuses(config_path)
        if not statuses:
            typer.echo("No configured accounts.")
            return
        for account in statuses:
            typer.echo(f"[{account.key}]")
            typer.echo(f"provider = {account.provider.value}")
            typer.echo(f"runtime = {config.accounts[account.key].runtime.value}")
            typer.echo(f"logged_in = {'yes' if account.logged_in else 'no'}")
            typer.echo(f"isolation_status = {account.isolation_status}")
            typer.echo(f"auth_storage = {account.auth_storage}")
            typer.echo(f"profile_root = {account.profile_root or '-'}")
            typer.echo(f"summary = {account.isolation_summary}")
            if account.isolation_recommendation:
                typer.echo(
                    f"recommendation = {account.isolation_recommendation}"
                )
            typer.echo("")

    @app.command("refresh-usage")
    def refresh_usage(
        account: str = typer.Argument(..., help="Account key or email."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        status = probe_account_usage(config_path, account)
        typer.echo(
            f"{status.key}: plan={status.plan} health={status.health} "
            f"usage={status.usage_summary}"
        )

    @app.command("tokens-sync")
    def tokens_sync(
        account: str | None = typer.Option(None, "--account", help="Optional account key or email to limit scanning."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        service = PollyPMService(config_path)
        count = service.sync_token_ledger(account=account)
        typer.echo(f"Synced {count} transcript token sample(s).")

    @app.command("tokens")
    def tokens(
        limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum rows to show."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        service = PollyPMService(config_path)
        rows = service.recent_token_usage(limit=limit)
        if not rows:
            typer.echo("No token usage recorded yet.")
            return
        for row in rows:
            typer.echo(
                f"- {row.hour_bucket} {row.project_key} {row.account_name} "
                f"{row.provider}/{row.model_name}: {row.tokens_used} tokens"
            )

    @app.command("costs")
    def costs(
        project: str | None = typer.Option(None, "--project", help="Filter by project key."),
        days: int = typer.Option(7, "--days", help="Look back N days."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Show token usage by project for the last N days."""
        config = load_config(config_path)
        store = StateStore(config.project.state_db)
        rows = store.execute(
            """
            SELECT project_key, SUM(tokens_used) as total,
                   SUM(cache_read_tokens) as cache_total,
                   COUNT(DISTINCT substr(hour_bucket, 1, 10)) as days_active
            FROM token_usage_hourly
            WHERE hour_bucket >= date('now', ?)
            GROUP BY project_key
            ORDER BY total DESC
            """,
            (f"-{days} days",),
        ).fetchall()
        store.close()
        if not rows:
            typer.echo("No token usage data.")
            return
        typer.echo(f"Token usage (last {days} days):\n")
        total_all = 0
        cache_all = 0
        for row in rows:
            proj_key, total, cache, days_active = (
                row[0],
                int(row[1]),
                int(row[2] or 0),
                int(row[3]),
            )
            if project and proj_key != project:
                continue
            cache_str = f" + {cache:,} cached" if cache else ""
            typer.echo(
                f"  {proj_key}: {total:,} tokens{cache_str} "
                f"({days_active} active day(s))"
            )
            total_all += total
            cache_all += cache
        cache_str = f" + {cache_all:,} cached" if cache_all else ""
        typer.echo(f"\n  Total: {total_all:,} tokens{cache_str}")

    @app.command("worktrees")
    def worktrees(
        project: str | None = typer.Option(None, "--project", help="Optional project key filter."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        items = list_project_worktrees(config_path, project)
        if not items:
            typer.echo("No tracked worktrees.")
            return
        for item in items:
            typer.echo(
                f"- {item.project_key} {item.lane_kind}/{item.lane_key}: "
                f"{item.path} [{item.branch}] status={item.status}"
            )

    @app.command()
    def add_account(
        provider: str = typer.Argument(..., help="Provider to add: codex or claude."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        provider_kind = ProviderKind(provider.lower())
        key, email = add_account_via_login(config_path, provider_kind)
        typer.echo(f"Added {email} as {key}")

    @app.command()
    def relogin(
        account: str = typer.Argument(..., help="Account key or email."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        key, email = relogin_account(config_path, account)
        typer.echo(f"Re-authenticated {email} ({key})")

    @app.command()
    def remove_account(
        account: str = typer.Argument(..., help="Account key or email."),
        delete_home: bool = typer.Option(False, "--delete-home", help="Also delete the isolated account home."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        key, email = remove_account_entry(
            config_path,
            account,
            delete_home=delete_home,
        )
        typer.echo(f"Removed {email} ({key})")

    @app.command()
    def repair(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        check_only: bool = typer.Option(False, "--check", help="Report problems without fixing."),
    ) -> None:
        """Check and repair PollyPM project scaffolding, docs, and state."""
        from pollypm import cli as cli_mod

        config_path = cli_mod._discover_config_path(config_path)
        if not config_path.exists():
            typer.echo(format_config_not_found_error(config_path), err=True)
            raise typer.Exit(code=1)
        config = load_config(config_path)
        all_problems: list[str] = []
        all_actions: list[str] = []

        global_problems = verify_docs(GLOBAL_CONFIG_DIR)
        if global_problems:
            for problem in global_problems:
                all_problems.append(f"[global] {problem}")
            if not check_only:
                actions = repair_docs(GLOBAL_CONFIG_DIR)
                for action in actions:
                    all_actions.append(f"[global] {action}")

        for key, project in config.projects.items():
            project_root = project.path
            if not project_root.exists():
                all_problems.append(
                    f"[{key}] project path does not exist: {project_root}"
                )
                continue

            state_dir = project_root / ".pollypm"
            for subdir in [
                "dossier",
                "logs",
                "artifacts",
                "checkpoints",
                "worktrees",
                "rules",
                "magic",
            ]:
                target = state_dir / subdir
                if target.exists():
                    continue
                all_problems.append(
                    f"[{key}] missing {target.relative_to(project_root)}"
                )
                if not check_only:
                    target.mkdir(parents=True, exist_ok=True)
                    all_actions.append(
                        f"[{key}] created {target.relative_to(project_root)}"
                    )

            doc_problems = verify_docs(project_root)
            for problem in doc_problems:
                all_problems.append(f"[{key}] {problem}")
            if not check_only and doc_problems:
                actions = repair_docs(project_root)
                for action in actions:
                    all_actions.append(f"[{key}] {action}")

            gitignore = project_root / ".gitignore"
            if gitignore.exists():
                content = gitignore.read_text()
                if ".pollypm/" not in content:
                    all_problems.append(
                        f"[{key}] .gitignore missing .pollypm/ entry"
                    )
                    if not check_only:
                        with gitignore.open("a") as handle:
                            if not content.endswith("\n"):
                                handle.write("\n")
                            handle.write(".pollypm/\n")
                        all_actions.append(
                            f"[{key}] added .pollypm/ to .gitignore"
                        )

        if not all_problems:
            typer.echo("All projects healthy. No repairs needed.")
            return
        typer.echo(f"Found {len(all_problems)} problem(s):")
        for problem in all_problems:
            typer.echo(f"  - {problem}")
        if check_only:
            typer.echo("\nRun `pm repair` (without --check) to fix.")
            return
        typer.echo(f"\nApplied {len(all_actions)} fix(es):")
        for action in all_actions:
            typer.echo(f"  + {action}")

    @app.command()
    def upgrade(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        check_only: bool = typer.Option(False, "--check", help="Only check if an upgrade is available."),
    ) -> None:
        """Check for and install PollyPM updates from GitHub."""
        import importlib.metadata

        try:
            current = importlib.metadata.version("pollypm")
        except importlib.metadata.PackageNotFoundError:
            current = "dev"

        try:
            result = subprocess.run(
                ["gh", "api", "repos/samhotchkiss/pollypm/releases/latest", "-q", ".tag_name"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["git", "ls-remote", "--tags", "https://github.com/samhotchkiss/pollypm.git"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    typer.echo("Could not check for updates. Are you online?")
                    raise typer.Exit(code=1)
                tags = [
                    line.split("refs/tags/")[-1]
                    for line in result.stdout.strip().splitlines()
                    if "refs/tags/" in line
                ]
                tags = [tag.lstrip("v") for tag in tags if not tag.endswith("^{}")]
                if not tags:
                    typer.echo(
                        f"Current version: {current}. No releases found on GitHub."
                    )
                    return
                latest = sorted(tags)[-1]
            else:
                latest = result.stdout.strip().lstrip("v")
        except FileNotFoundError:
            typer.echo("Neither `gh` nor `git` found. Cannot check for updates.")
            raise typer.Exit(code=1)

        typer.echo(f"Current: {current}")
        typer.echo(f"Latest:  {latest}")

        if current == latest or current == "dev":
            if current == "dev":
                typer.echo("Running from source (dev). Use `git pull` to update.")
            else:
                typer.echo("Already up to date.")
            if not check_only:
                typer.echo("\nRegenerating docs from current templates...")
                config = load_config(config_path)
                repair_docs(GLOBAL_CONFIG_DIR)
                for key, project in config.projects.items():
                    if not project.path.exists():
                        continue
                    actions = repair_docs(project.path)
                    if actions:
                        typer.echo(f"  [{key}] {len(actions)} doc(s) updated")
                typer.echo("Done.")
            return

        if check_only:
            typer.echo(f"\nUpgrade available: {current} -> {latest}")
            typer.echo("Run `pm upgrade` to install.")
            return

        typer.echo(f"\nUpgrading {current} -> {latest}...")
        uv = shutil.which("uv")
        if uv:
            pip_cmd = [uv, "pip", "install", "--upgrade", f"pollypm=={latest}"]
        else:
            pip_cmd = ["pip", "install", "--upgrade", f"pollypm=={latest}"]

        install_result = subprocess.run(pip_cmd, capture_output=True, text=True)
        if install_result.returncode != 0:
            typer.echo("PyPI install failed, trying GitHub source...")
            if uv:
                pip_cmd = [
                    uv,
                    "pip",
                    "install",
                    f"git+https://github.com/samhotchkiss/pollypm.git@v{latest}",
                ]
            else:
                pip_cmd = [
                    "pip",
                    "install",
                    f"git+https://github.com/samhotchkiss/pollypm.git@v{latest}",
                ]
            install_result = subprocess.run(
                pip_cmd,
                capture_output=True,
                text=True,
            )
            if install_result.returncode != 0:
                typer.echo(f"Upgrade failed:\n{install_result.stderr}")
                raise typer.Exit(code=1)

        typer.echo("Package updated. Regenerating docs...")
        config = load_config(config_path)
        repair_docs(GLOBAL_CONFIG_DIR)
        for key, project in config.projects.items():
            if not project.path.exists():
                continue
            actions = repair_docs(project.path)
            if actions:
                typer.echo(f"  [{key}] {len(actions)} doc(s) updated")
        typer.echo(
            f"Upgrade to {latest} complete. Running sessions are unaffected — "
            "restart with `pm reset && pm up` when ready."
        )

    @app.command("backup")
    def backup_cmd(
        output: Path | None = typer.Option(
            None,
            "--output",
            "-o",
            help="Custom destination path for the snapshot. Default: ~/.pollypm/backups/state-db-<ts>.db.gz",
        ),
        full: bool = typer.Option(
            False,
            "--full",
            help="Bundle state.db + ~/.pollypm/ tree into a single .tar.gz. Not subject to retention.",
        ),
        keep: int = typer.Option(
            None,
            "--keep",
            help="Keep N most recent DB snapshots; prune the rest. Ignored with --full.",
        ),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Snapshot ~/.pollypm/state.db using SQLite's online backup API."""
        from pollypm import backup as backup_mod

        try:
            config = load_config(config_path)
        except FileNotFoundError:
            typer.echo(
                f"Config not found at {config_path}. Run `pm` to onboard first.",
                err=True,
            )
            raise typer.Exit(code=1)

        state_db = config.project.state_db
        base_dir = config.project.base_dir
        keep_value = backup_mod.DEFAULT_KEEP if keep is None else keep

        try:
            result = backup_mod.backup_state_db(
                state_db,
                base_dir=base_dir,
                output=output,
                full=full,
                keep=keep_value,
            )
        except FileNotFoundError as exc:
            typer.echo(f"Backup failed: {exc}", err=True)
            raise typer.Exit(code=1)
        except Exception as exc:
            typer.echo(f"Backup failed: {exc}", err=True)
            raise typer.Exit(code=1)

        before = backup_mod.humanize_bytes(result.db_size_before)
        after = backup_mod.humanize_bytes(result.archive_size)
        typer.echo(
            f"Backed up to {result.path}. DB size before: {before}. "
            f"Archive size: {after}."
        )
        if result.pruned:
            typer.echo(
                f"Pruned {len(result.pruned)} older snapshot(s) (keep={keep_value})."
            )

    @app.command("restore")
    def restore_cmd(
        snapshot_path: Path = typer.Argument(..., help="Path to a .db.gz DB snapshot or --full .tar.gz archive."),
        confirm: bool = typer.Option(
            False,
            "--confirm",
            help="Required to actually replace the live DB. Without this flag, restore refuses.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Describe what would happen without touching any files.",
        ),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Restore state.db from a snapshot produced by ``pm backup``."""
        from pollypm import backup as backup_mod
        from pollypm.session_services import probe_session

        try:
            config = load_config(config_path)
        except FileNotFoundError:
            typer.echo(
                f"Config not found at {config_path}. Run `pm` to onboard first.",
                err=True,
            )
            raise typer.Exit(code=1)

        live_db = config.project.state_db

        try:
            plan = backup_mod.plan_restore(snapshot_path, live_db)
        except FileNotFoundError as exc:
            typer.echo(f"Restore failed: {exc}", err=True)
            raise typer.Exit(code=1)
        except ValueError as exc:
            typer.echo(f"Restore failed: {exc}", err=True)
            raise typer.Exit(code=1)

        if dry_run:
            typer.echo("Dry run — no files will be touched.")
            typer.echo(f"  snapshot:      {plan.snapshot_path}")
            typer.echo(f"  live DB:       {plan.live_db_path}")
            typer.echo(f"  safety copy:   {plan.safety_path}")
            typer.echo(
                f"  snapshot kind: {'full tar.gz' if plan.is_tar else 'db snapshot'}"
            )
            return

        if not confirm:
            session_name = config.project.tmux_session
            typer.echo("Restore refused — this replaces the live state.db.")
            typer.echo("")
            typer.echo("Before you proceed:")
            typer.echo(
                f"  1. Stop the cockpit: `tmux kill-session -t {session_name}` "
                "(or `pm reset`)."
            )
            typer.echo("  2. Re-run with `--confirm`:")
            typer.echo(f"       pm restore {snapshot_path} --confirm")
            typer.echo("")
            typer.echo(
                "A safety copy of the live DB will be written to "
                f"{plan.safety_path} before anything is replaced."
            )
            raise typer.Exit(code=1)

        try:
            if probe_session(config.project.tmux_session):
                typer.echo(
                    f"WARNING: tmux session '{config.project.tmux_session}' "
                    "appears to still be running. Stop it first or the "
                    "restored DB may be overwritten by the live cockpit."
                )
        except Exception:
            pass

        try:
            result = backup_mod.execute_restore(plan)
        except Exception as exc:
            typer.echo(f"Restore failed mid-flight: {exc}", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"Restored {result.live_db_path} from {result.snapshot_path}.")
        typer.echo(f"Safety copy of the previous live DB: {result.safety_path}")
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo("  pm up                  # relaunch the cockpit")
        typer.echo("  pm doctor              # verify the restored state")
