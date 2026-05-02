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

import json
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    GLOBAL_CONFIG_DIR,
    load_config,
)
from pollypm.doc_scaffold import repair_docs, verify_docs
from pollypm.errors import format_config_not_found_error
from pollypm.models import ProviderKind
from pollypm.storage.state import StateStore
from pollypm.worktrees import list_worktrees as list_project_worktrees


@dataclass(frozen=True)
class RepairProblem:
    """A single problem found by ``pm repair``.

    ``project`` is the project key (or ``"global"`` for global config).
    ``klass`` is one of ``missing``, ``outdated``, ``config`` — the
    aggregation key alongside ``group``. ``path`` is the literal target
    path (relative to the project root) for messaging; ``group`` is the
    aggregation bucket — usually equal to ``path`` but the per-doc
    ``.pollypm/docs/reference/<file>.md`` problems collapse to a single
    family ``.pollypm/docs/reference/``.
    """

    project: str
    klass: str
    path: str
    group: str


@dataclass
class _Bucket:
    klass: str
    group: str
    paths: set[str] = field(default_factory=set)
    projects: list[str] = field(default_factory=list)
    project_set: set[str] = field(default_factory=set)
    count: int = 0

    def add(self, problem: RepairProblem) -> None:
        self.paths.add(problem.path)
        self.count += 1
        if problem.project not in self.project_set:
            self.project_set.add(problem.project)
            self.projects.append(problem.project)


def _aggregate_problems(
    problems: list[RepairProblem],
) -> "OrderedDict[tuple[str, str], _Bucket]":
    """Group problems by ``(klass, group)`` preserving insertion order."""
    buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()
    for problem in problems:
        key = (problem.klass, problem.group)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _Bucket(klass=problem.klass, group=problem.group)
            buckets[key] = bucket
        bucket.add(problem)
    return buckets


def _format_aggregated_report(
    problems: list[RepairProblem],
    *,
    sample_limit: int = 3,
) -> list[str]:
    """Render the aggregated, doctor-styled ``pm repair --check`` block.

    Returns a list of lines. The caller adds the trailing summary +
    next-action hints so dry-run / check / fix paths can vary the
    final stanza.
    """
    buckets = _aggregate_problems(problems)
    distinct_projects: set[str] = set()
    for bucket in buckets.values():
        distinct_projects.update(bucket.project_set)
    distinct_projects.discard("global")
    project_count = len(distinct_projects)
    total = len(problems)
    project_word = "project" if project_count == 1 else "projects"
    problem_word = "problem" if total == 1 else "problems"

    lines: list[str] = [
        f"Found {total} {problem_word} across {project_count} {project_word}:"
    ]

    label_width = 56
    for bucket in buckets.values():
        if (
            bucket.klass == "outdated"
            and bucket.group == ".pollypm/docs/reference/"
        ):
            doc_count = len(bucket.paths)
            doc_word = "doc" if doc_count == 1 else "docs"
            label = (
                f"  [outdated] .pollypm/docs/reference/"
                f"<{doc_count} {doc_word} each>"
            )
        else:
            label = f"  [{bucket.klass}] {bucket.group}"
        n_projects = len(bucket.project_set)
        proj_word = "project" if n_projects == 1 else "projects"
        if bucket.count == n_projects:
            right = f"{n_projects} {proj_word}"
        else:
            right = f"{bucket.count} / {n_projects} {proj_word}"
        padding = max(1, label_width - len(label))
        lines.append(f"{label}{' ' * padding}{right}")

        sample = bucket.projects[:sample_limit]
        more = len(bucket.projects) - len(sample)
        sample_str = ", ".join(sample)
        if more > 0:
            sample_str = f"{sample_str} (+{more} more)"
        lines.append(f"    {sample_str}")

    return lines


def _format_summary_line(problems: list[RepairProblem]) -> str:
    """Build the trailing one-line summary."""
    buckets = _aggregate_problems(problems)
    distinct_projects: set[str] = set()
    for bucket in buckets.values():
        distinct_projects.update(bucket.project_set)
    distinct_projects.discard("global")
    project_count = len(distinct_projects)
    n_classes = len(buckets)
    total = len(problems)
    problem_word = "problem" if total == 1 else "problems"
    project_word = "project" if project_count == 1 else "projects"
    class_word = "class" if n_classes == 1 else "classes"
    return (
        f"Summary: {total} {problem_word} across {project_count} "
        f"{project_word} ({n_classes} {class_word}, all auto-fixable)"
    )


def _problem_group(klass: str, path: str) -> str:
    """Aggregation key for a problem.

    Reference docs collapse into one ``.pollypm/docs/reference/`` family;
    everything else groups by its literal path.
    """
    if path.startswith(".pollypm/docs/reference/"):
        return ".pollypm/docs/reference/"
    return path


def _split_doc_problem(message: str) -> tuple[str, str]:
    """Parse a ``verify_docs`` message of the form ``"<klass> <path>"``.

    ``verify_docs`` returns strings like ``"missing .pollypm/docs/SYSTEM.md"``
    or ``"outdated .pollypm/docs/reference/sessions.md"``. Falls back to
    a ``("config", message)`` tuple if the prefix doesn't match — defensive
    against any future verifier additions.
    """
    for prefix in ("missing ", "outdated "):
        if message.startswith(prefix):
            return prefix.strip(), message[len(prefix):]
    return "config", message

debug_app = typer.Typer(help="Low-level debugging helpers.")


def _semver_sort_key(tag: str):
    """Return a sort key that orders ``tag`` by semver when possible.

    Used by ``pm upgrade``'s ``git ls-remote`` fallback path to pick
    the actual newest release rather than the lexicographically last
    tag (which puts ``1.9.0`` after ``1.10.0``). Tags that don't
    parse as a valid PEP 440 version sort before any parseable
    version so a stray non-semver tag (e.g. ``nightly``) never
    masquerades as the latest release.
    """
    from packaging.version import InvalidVersion, Version

    try:
        return (1, Version(tag), "")
    except InvalidVersion:
        # Tuple shape matches the parseable branch so heterogeneous
        # comparisons don't TypeError on the Version field.
        return (0, Version("0"), tag)


def _service(config_path: Path):
    from pollypm.service_api import PollyPMService

    return PollyPMService(config_path)


def _list_account_statuses(config_path: Path):
    from pollypm.accounts import list_account_statuses

    return list_account_statuses(config_path)


def _probe_account_usage(config_path: Path, account: str):
    from pollypm.accounts import probe_account_usage

    return probe_account_usage(config_path, account)


def _add_account_via_login(config_path: Path, provider_kind: ProviderKind):
    from pollypm.accounts import add_account_via_login

    return add_account_via_login(config_path, provider_kind)


def _relogin_account(config_path: Path, account: str):
    from pollypm.accounts import relogin_account

    return relogin_account(config_path, account)


def _remove_account_entry(config_path: Path, account: str, *, delete_home: bool = False):
    from pollypm.accounts import remove_account as remove_account_entry

    return remove_account_entry(config_path, account, delete_home=delete_home)


def register_maintenance_commands(app: typer.Typer) -> None:
    @app.command(
        help=help_with_examples(
            "Run the diagnostic checklist and optionally apply safe fixes.",
            [
                ("pm doctor", "show the full health checklist"),
                ("pm doctor --fix", "apply safe automatic repairs"),
                ("pm doctor --json", "emit the report as machine-readable JSON"),
            ],
        )
    )
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
            release_channel_line,
            render_fix_dry_run,
            render_fix_summary,
            render_human,
            render_json,
            run_checks,
            setup_tag_line,
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
            from pollypm.release_check import update_banner_line

            banner = update_banner_line()
            if banner:
                typer.echo(banner)
                typer.echo("")
            typer.echo(render_human(report))
            typer.echo("")
            typer.echo(release_channel_line())
            typer.echo(setup_tag_line())
        if fix_summary:
            typer.echo("")
            typer.echo(fix_summary)
        raise typer.Exit(code=0 if report.ok else 1)

    @app.command(
        help=(
            "List configured provider accounts with login state, "
            "isolation, and usage summary."
        ),
    )
    def accounts(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        for account in _list_account_statuses(config_path):
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

    @app.command()
    def errors(
        raw: bool = typer.Option(
            False, "--raw",
            help=(
                "Stream the literal ~/.pollypm/errors.log file (the pre-#1040 "
                "behavior). Implied by --tail/--follow/--grep."
            ),
        ),
        tail: int = typer.Option(
            50, "--tail", "-n",
            help="Raw-mode only: show the last N lines (0 = whole file).",
        ),
        follow: bool = typer.Option(
            False, "--follow", "-f",
            help="Raw-mode only: follow the log as new records land.",
        ),
        grep: str = typer.Option(
            "", "--grep", "-g",
            help="Raw-mode only: filter lines to those containing this substring.",
        ),
        since: str = typer.Option(
            "24h", "--since",
            help=(
                "Summary window for ERROR-source rollup (e.g. 1h, 24h, 7d). "
                "Ignored in raw mode."
            ),
        ),
        fingerprint: str = typer.Option(
            "", "--fingerprint",
            help=(
                "Expand one fingerprint hash (12-char prefix from "
                "`error_log/critical_error:<hash>`) to its full traceback."
            ),
        ),
    ) -> None:
        """Triage view over ``~/.pollypm/errors.log``.

        Default (no flags) prints a summary: top ERROR sources by
        ``pollypm.<module>`` over the last ``--since`` window plus
        active fingerprints (>= 3 occurrences in the last hour) so
        the user can see "what's broken" without scanning 40k log
        lines. ``--raw`` (or ``--tail/--follow/--grep``) restores the
        literal file dump for ``grep``-style spelunking.
        """
        import subprocess as _sp
        from datetime import datetime
        from pollypm.error_log import path as _error_log_path
        from pollypm import error_log_summary as _els

        log_path = _error_log_path()
        if not log_path.exists():
            typer.echo(f"No error log yet at {log_path}. All quiet.")
            raise typer.Exit(code=0)

        # ``--tail/--follow/--grep`` are raw-mode features. If any of
        # them are set, keep the legacy behavior even without ``--raw``
        # so existing muscle memory keeps working.
        raw_mode_requested = (
            raw or follow or bool(grep) or tail != 50
        )

        if not raw_mode_requested and not fingerprint:
            try:
                window = _els.parse_duration(since)
            except ValueError as exc:
                typer.echo(f"Invalid --since value: {exc}", err=True)
                raise typer.Exit(code=2)
            records = _els.read_log(log_path)
            now = datetime.now()
            summary = _els.summarize(
                records,
                now=now,
                since=window,
                window_label=since,
            )
            typer.echo(_els.render_summary(summary, now=now))
            raise typer.Exit(code=0)

        if fingerprint:
            records = _els.read_log(log_path)
            matches = _els.find_fingerprint_records(records, fingerprint)
            typer.echo(_els.render_fingerprint(matches, fingerprint))
            raise typer.Exit(code=0 if matches else 1)

        # Raw mode — preserve the original tail/follow/grep behavior.
        cmd: list[str] = ["tail"]
        if tail <= 0:
            cmd = ["cat", str(log_path)]
        else:
            cmd += ["-n", str(tail)]
            if follow:
                cmd.append("-F")
            cmd.append(str(log_path))
        if grep:
            # Pipe through grep when a filter is requested. Keep the
            # exit status from grep so "no matches" returns 1 to the
            # shell per grep convention.
            proc1 = _sp.Popen(cmd, stdout=_sp.PIPE)
            proc2 = _sp.Popen(["grep", "--line-buffered", grep], stdin=proc1.stdout)
            proc1.stdout.close()  # allow SIGPIPE to reach proc1 if proc2 exits
            raise typer.Exit(code=proc2.wait())
        raise typer.Exit(code=_sp.call(cmd))

    @app.command(
        "account-doctor",
        help=(
            "Per-account health detail (provider, runtime, isolation, "
            "auth storage). Use to debug why an account isn't picking "
            "up sessions."
        ),
    )
    def account_doctor(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        config = load_config(config_path)
        statuses = _list_account_statuses(config_path)
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

    @app.command(
        "refresh-usage",
        help="Probe one account's plan/usage and print the refreshed summary.",
    )
    def refresh_usage(
        account: str = typer.Argument(..., help="Account key or email."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        status = _probe_account_usage(config_path, account)
        typer.echo(
            f"{status.key}: plan={status.plan} health={status.health} "
            f"usage={status.usage_summary}"
        )

    @app.command(
        "tokens-sync",
        help=(
            "Walk transcripts and ingest token-usage samples into the "
            "ledger. Used after relaunching from a backup or syncing "
            "across machines."
        ),
    )
    def tokens_sync(
        account: str | None = typer.Option(None, "--account", help="Optional account key or email to limit scanning."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        service = _service(config_path)
        count = service.sync_token_ledger(account=account)
        sample_word = "sample" if count == 1 else "samples"
        typer.echo(f"Synced {count} transcript token {sample_word}.")

    @app.command(
        "tokens",
        help="List the most recent token-usage samples (one row per hour bucket).",
    )
    def tokens(
        limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum rows to show."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        service = _service(config_path)
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
        # #1042 — group on ``LOWER(project_key)`` so historical writers
        # that emitted both ``"pollypm"`` and ``"PollyPM"`` (the display
        # name leaked through ``_project_key_for_cwd`` fallback in older
        # versions) collapse into one row. Defense-in-depth on top of
        # the writer-side normalization in ``transcript_ledger``.
        rows = store.execute(
            """
            SELECT LOWER(project_key) AS project_key,
                   SUM(tokens_used) as total,
                   SUM(cache_read_tokens) as cache_total,
                   COUNT(DISTINCT substr(hour_bucket, 1, 10)) as days_active
            FROM token_usage_hourly
            WHERE hour_bucket >= date('now', ?)
            GROUP BY LOWER(project_key)
            ORDER BY total DESC
            """,
            (f"-{days} days",),
        ).fetchall()
        store.close()
        if not rows:
            typer.echo("No token usage data.")
            return
        days_word = "day" if days == 1 else "days"
        typer.echo(f"Token usage (last {days} {days_word}):\n")
        total_all = 0
        cache_all = 0
        for row in rows:
            proj_key, total, cache, days_active = (
                row[0],
                int(row[1]),
                int(row[2] or 0),
                int(row[3]),
            )
            # #1042 — proj_key is already lowercased by the SQL above;
            # case-fold the user's filter so ``--project PollyPM`` and
            # ``--project pollypm`` both match a single canonical row.
            if project and proj_key != project.lower():
                continue
            cache_str = f" + {cache:,} cached" if cache else ""
            day_word = "day" if days_active == 1 else "days"
            typer.echo(
                f"  {proj_key}: {total:,} tokens{cache_str} "
                f"({days_active} active {day_word})"
            )
            total_all += total
            cache_all += cache
        cache_str = f" + {cache_all:,} cached" if cache_all else ""
        typer.echo(f"\n  Total: {total_all:,} tokens{cache_str}")

    @app.command(
        "worktrees",
        help="List per-task git worktrees PollyPM is tracking.",
    )
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

    @app.command(
        help=(
            "Authenticate a new provider account (Claude or Codex) "
            "and register it in PollyPM."
        ),
    )
    def add_account(
        provider: str = typer.Argument(..., help="Provider to add: codex or claude."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        provider_kind = ProviderKind(provider.lower())
        key, email = _add_account_via_login(config_path, provider_kind)
        typer.echo(f"Added {email} as {key}")

    @app.command(
        help=(
            "Re-run provider login for an existing account so PollyPM "
            "picks up a fresh session token."
        ),
    )
    def relogin(
        account: str = typer.Argument(..., help="Account key or email."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        key, email = _relogin_account(config_path, account)
        typer.echo(f"Re-authenticated {email} ({key})")

    @app.command(
        help=(
            "Unregister a provider account from PollyPM. Optionally "
            "wipes the isolated account home directory too."
        ),
    )
    def remove_account(
        account: str = typer.Argument(..., help="Account key or email."),
        delete_home: bool = typer.Option(False, "--delete-home", help="Also delete the isolated account home."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        key, email = _remove_account_entry(
            config_path,
            account,
            delete_home=delete_home,
        )
        typer.echo(f"Removed {email} ({key})")

    @app.command()
    def repair(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        check_only: bool = typer.Option(False, "--check", help="Report problems without fixing."),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what `pm repair` WOULD write, without mutating anything.",
        ),
    ) -> None:
        """Check and repair PollyPM project scaffolding, docs, and state."""
        from pollypm import cli as cli_mod

        if check_only and dry_run:
            typer.echo(
                "--check and --dry-run are mutually exclusive: --check reports "
                "problems, --dry-run previews fixes.",
                err=True,
            )
            raise typer.Exit(code=2)

        config_path = cli_mod._discover_config_path(config_path)
        if not config_path.exists():
            typer.echo(format_config_not_found_error(config_path), err=True)
            raise typer.Exit(code=1)
        config = load_config(config_path)
        all_problems: list[RepairProblem] = []
        all_actions: list[str] = []
        # ``apply`` is False for both --check and --dry-run; the difference
        # is only in the trailing message.
        apply = not (check_only or dry_run)

        global_problems = verify_docs(GLOBAL_CONFIG_DIR)
        if global_problems:
            for problem in global_problems:
                klass, path = _split_doc_problem(problem)
                all_problems.append(
                    RepairProblem(
                        project="global",
                        klass=klass,
                        path=path,
                        group=_problem_group(klass, path),
                    )
                )
            if apply:
                actions = repair_docs(GLOBAL_CONFIG_DIR)
                for action in actions:
                    all_actions.append(f"[global] {action}")

        for key, project in config.projects.items():
            project_root = project.path
            if not project_root.exists():
                msg = f"project path does not exist: {project_root}"
                all_problems.append(
                    RepairProblem(
                        project=key,
                        klass="config",
                        path=msg,
                        group=_problem_group("config", msg),
                    )
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
                rel = str(target.relative_to(project_root))
                all_problems.append(
                    RepairProblem(
                        project=key,
                        klass="missing",
                        path=rel,
                        group=_problem_group("missing", rel),
                    )
                )
                if apply:
                    target.mkdir(parents=True, exist_ok=True)
                    all_actions.append(f"[{key}] created {rel}")

            doc_problems = verify_docs(project_root)
            for problem in doc_problems:
                klass, path = _split_doc_problem(problem)
                all_problems.append(
                    RepairProblem(
                        project=key,
                        klass=klass,
                        path=path,
                        group=_problem_group(klass, path),
                    )
                )
            if apply and doc_problems:
                actions = repair_docs(project_root)
                for action in actions:
                    all_actions.append(f"[{key}] {action}")

            gitignore = project_root / ".gitignore"
            if gitignore.exists():
                content = gitignore.read_text()
                if ".pollypm/" not in content:
                    rel = ".gitignore missing .pollypm/ entry"
                    all_problems.append(
                        RepairProblem(
                            project=key,
                            klass="config",
                            path=rel,
                            group=_problem_group("config", rel),
                        )
                    )
                    if apply:
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

        for line in _format_aggregated_report(all_problems):
            typer.echo(line)

        if check_only:
            typer.echo("")
            typer.echo(
                "Run `pm repair` to fix all (safe; rewrites scaffolding only)."
            )
            typer.echo("Run `pm repair --dry-run` to preview writes.")
            typer.echo("")
            typer.echo(_format_summary_line(all_problems))
            return

        if dry_run:
            typer.echo("")
            typer.echo("Dry-run; no writes performed.")
            typer.echo("Run `pm repair` to apply.")
            typer.echo("")
            typer.echo(_format_summary_line(all_problems))
            return

        fix_word = "fix" if len(all_actions) == 1 else "fixes"
        typer.echo("")
        typer.echo(f"Applied {len(all_actions)} {fix_word}:")
        for action in all_actions:
            typer.echo(f"  + {action}")
        typer.echo("")
        typer.echo(_format_summary_line(all_problems))

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
                # Sort by semver, not lexicographically — otherwise the
                # ``git ls-remote`` fallback picks ``1.9.0`` over ``1.10.0``
                # the moment the project crosses the v1.10 line. The ``gh``
                # API path above already returns the latest by release time.
                latest = sorted(tags, key=_semver_sort_key)[-1]
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
                        doc_word = "doc" if len(actions) == 1 else "docs"
                        typer.echo(f"  [{key}] {len(actions)} {doc_word} updated")
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
                doc_word = "doc" if len(actions) == 1 else "docs"
                typer.echo(f"  [{key}] {len(actions)} {doc_word} updated")
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
            snap_word = "snapshot" if len(result.pruned) == 1 else "snapshots"
            typer.echo(
                f"Pruned {len(result.pruned)} older {snap_word} "
                f"(keep={keep_value})."
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


@debug_app.command(
    "decode-setup-tag",
    help=(
        "Decode a setup tag (the short hex from ``pm doctor``'s "
        "footer) into the underlying environment fingerprint JSON."
    ),
)
def decode_setup_tag(
    tag: str = typer.Argument(..., help="The 6-8 hex setup tag to decode."),
) -> None:
    from pollypm.doctor import decode_setup_tag as _decode_setup_tag

    fingerprint = _decode_setup_tag(tag)
    if fingerprint is None:
        typer.echo(f"Unknown setup tag: {tag}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(fingerprint, indent=2, sort_keys=True))
