"""Install-state checks extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import shlex

import pollypm.doctor as doctor


def check_pm_binary_resolves() -> doctor.CheckResult:
    path = doctor._tool_path("pm") or doctor._tool_path("pollypm")
    if path is None:
        return doctor._fail(
            "pm / pollypm binary not on PATH",
            why=(
                "The canonical entry point is the `pm` command installed by "
                "`uv tool install --editable .`. Without it, every user-facing "
                "workflow requires `uv run pm ...`."
            ),
            fix=(
                "Install the global entry points —\n"
                "  uv tool install --editable .\n"
                "Or run via uv:  uv run pm doctor\n"
                "Recheck: pm doctor"
            ),
            auto_fix=doctor._reinstall_editable_auto_fix("Install PollyPM editable"),
        )
    return doctor._ok(f"pm binary at {path}", data={"path": path})


def check_installed_version_matches_pyproject() -> doctor.CheckResult:
    """Warn when the installed package version drifts from pyproject.toml."""
    declared = doctor._read_pyproject_version()
    if not declared:
        return doctor._skip("pyproject.toml version not readable")
    try:
        from importlib.metadata import PackageNotFoundError, version as _mdver

        installed = _mdver("pollypm")
    except PackageNotFoundError:
        return doctor._fail(
            "pollypm package metadata not found",
            why=(
                "PollyPM must be installed (editable or otherwise) for the "
                "CLI entry points to resolve. `uv run pm ...` still works, "
                "but first-class `pm` requires the install step."
            ),
            fix=(
                "Install PollyPM editable —\n"
                "  uv tool install --editable .\n"
                "Recheck: pm doctor"
            ),
            auto_fix=doctor._reinstall_editable_auto_fix("Install PollyPM editable"),
        )
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"package metadata unreadable ({exc})")
    if installed != declared:
        return doctor._fail(
            f"installed pollypm={installed} drifts from source {declared}",
            why=(
                "An editable install can fall behind after `git pull` if the "
                "entry point was reinstalled from a prior revision. Running "
                "`pm ...` may execute stale code."
            ),
            fix=(
                "Reinstall the editable package —\n"
                "  uv tool install --editable --reinstall .\n"
                "Or:  uv sync --reinstall\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"installed": installed, "source": declared},
            auto_fix=doctor._reinstall_editable_auto_fix("Reinstall the editable PollyPM package"),
        )
    return doctor._ok(f"pollypm {installed} matches pyproject", data={"version": installed})


def check_deploy_source_staleness() -> doctor.CheckResult:
    """Warn when a uv-tool copy is stale relative to its recorded checkout."""
    try:
        from pollypm.deploy_info import assess_deploy_staleness
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"deploy-staleness check skipped (import failed: {exc})")

    try:
        status = assess_deploy_staleness()
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"deploy-staleness check skipped ({exc})")

    if status.state == "unknown":
        return doctor._skip(status.status)
    if status.state == "ok":
        return doctor._ok(status.status, data=status.data)

    source = status.data.get("source_checkout")
    install_cmd = "uv tool install --force pollypm"
    if isinstance(source, str) and source:
        install_cmd = f"uv tool install --force --from {shlex.quote(source)} pollypm"
    return doctor._fail(
        status.status,
        why=(
            f"{status.reason} This can make a restarted `pm serve` keep "
            "running old code even after the source checkout has advanced."
        ),
        fix=(
            "Reinstall the uv tool from the current source checkout, then "
            "restart any running pm serve process —\n"
            "  uv cache clean pollypm\n"
            f"  {install_cmd}\n"
            "  pm serve stop && pm serve start   # launchd-managed server\n"
            "Recheck: pm doctor"
        ),
        severity="warning",
        data=status.data,
    )


def check_config_file() -> doctor.CheckResult:
    from pollypm.config import DEFAULT_CONFIG_PATH

    if DEFAULT_CONFIG_PATH.exists():
        return doctor._ok(f"config present at {DEFAULT_CONFIG_PATH}", data={"path": str(DEFAULT_CONFIG_PATH)})
    return doctor._fail(
        f"no PollyPM config at {DEFAULT_CONFIG_PATH}",
        why=(
            "PollyPM loads accounts, sessions, and project settings from "
            "~/.pollypm/pollypm.toml. Without it the CLI runs first-run "
            "onboarding every time."
        ),
        fix=(
            "Run onboarding or scaffold an example config —\n"
            "  pm onboard\n"
            "Or:  pm init\n"
            "Recheck: pm doctor"
        ),
    )


def check_provider_account_configured() -> doctor.CheckResult:
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("account check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            f"config failed to parse ({exc})",
            why=(
                "A broken ~/.pollypm/pollypm.toml prevents the CLI from "
                "loading any accounts or sessions."
            ),
            fix=(
                "Inspect and fix the config —\n"
                "  pm example-config   # reference template\n"
                "  edit ~/.pollypm/pollypm.toml\n"
                "Recheck: pm doctor"
            ),
            data={"error": str(exc)},
        )
    accounts = getattr(config, "accounts", {}) or {}
    if not accounts:
        return doctor._fail(
            "no provider accounts configured",
            why=(
                "PollyPM needs at least one Claude or Codex account to launch "
                "agent sessions. Heartbeat, workers, and cockpit all require "
                "a provider-bound account."
            ),
            fix=(
                "Add an account via onboarding —\n"
                "  pm onboard\n"
                "Or edit ~/.pollypm/pollypm.toml and add an [accounts.*] block\n"
                "(see `pm example-config`).\n"
                "Recheck: pm doctor"
            ),
        )
    account_word = "account" if len(accounts) == 1 else "accounts"
    return doctor._ok(
        f"{len(accounts)} provider {account_word} configured",
        data={"accounts": sorted(accounts.keys())},
    )


def check_storage_backend() -> doctor.CheckResult:
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config
    from pollypm.errors import StoreBackendNotFound
    from pollypm.store.registry import get_store

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("storage backend check skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            f"config failed to parse ({exc})",
            why=(
                "Doctor cannot resolve the storage backend without a "
                "parseable ~/.pollypm/pollypm.toml."
            ),
            fix=(
                "Inspect and fix the config —\n"
                "  pm example-config   # reference template\n"
                "  edit ~/.pollypm/pollypm.toml\n"
                "Recheck: pm doctor"
            ),
            data={"error": str(exc)},
        )
    backend_name = config.storage.backend
    try:
        store = get_store(config)
    except StoreBackendNotFound as exc:
        return doctor._fail(
            f"storage backend '{backend_name}' not installed",
            why=(
                "PollyPM resolves its persistent-state backend via the "
                "'pollypm.store_backend' entry-point group; no installed "
                "package registered that name. Every subsystem that writes "
                "state will fail until this is fixed."
            ),
            fix=(
                "Set [storage].backend in ~/.pollypm/pollypm.toml to an "
                "installed backend, or install the package that ships the "
                f"'{backend_name}' backend.\n"
                f"Available: {', '.join(exc.available) or 'none'}\n"
                "Recheck: pm doctor"
            ),
            data={"backend": backend_name, "available": exc.available},
        )
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            f"storage backend '{backend_name}' failed to initialise ({exc})",
            why=(
                "The entry point loaded but construction raised — the "
                "backend is registered but not usable in this environment."
            ),
            fix=(
                "Inspect the error above, check [storage].url in "
                "~/.pollypm/pollypm.toml, and verify the backend package's "
                "own dependencies.\n"
                "Recheck: pm doctor"
            ),
            data={"backend": backend_name, "error": str(exc)},
        )
    url = getattr(store, "url", "<unknown>")
    try:
        dispose = getattr(store, "dispose", None)
        if callable(dispose):
            dispose()
    except Exception:  # noqa: BLE001
        pass
    return doctor._ok(
        f"storage backend '{backend_name}' active at {url}",
        data={"backend": backend_name, "url": url},
    )


def check_registered_providers() -> doctor.CheckResult:
    from pollypm.acct import get_provider, list_providers

    names = list_providers()
    if not names:
        return doctor._fail(
            "no providers registered",
            why=(
                "PollyPM resolves every account (Claude, Codex, plugin) "
                "via the 'pollypm.provider' entry-point group; with no "
                "entry points registered, no account can run and every "
                "`pm account` command will fail."
            ),
            fix=(
                "Reinstall PollyPM so the built-in 'claude' and 'codex' "
                "entry points are registered —\n"
                "  uv tool install --editable --reinstall .\n"
                "If a third-party provider is expected, reinstall the "
                "plugin package that ships it.\n"
                "Recheck: pm doctor"
            ),
            data={"providers": []},
        )

    failures: dict[str, str] = {}
    for name in names:
        try:
            get_provider(name)
        except Exception as exc:  # noqa: BLE001
            failures[name] = f"{type(exc).__name__}: {exc}"

    if failures:
        first_name = next(iter(failures))
        first_error = failures[first_name]
        return doctor._fail(
            f"{first_name} failed to load ({first_error})",
            why=(
                "A registered provider adapter raised on import or "
                "instantiation. Every account whose provider string "
                "maps to that adapter will fail before any subprocess "
                "runs — the failure is silent until the user actually "
                "probes an account."
            ),
            fix=(
                "Inspect the error above, verify the provider plugin's "
                "installation, and fix the import / constructor issue.\n"
                f"Registered providers: {', '.join(names)}\n"
                f"Failing: {', '.join(sorted(failures))}\n"
                "Recheck: pm doctor"
            ),
            data={"providers": names, "failures": failures},
        )

    return doctor._ok(
        f"registered-providers: {', '.join(names)}",
        data={"providers": names},
    )


def _parse_pg_server_version(raw: object) -> tuple[int, int] | None:
    """Parse pg's numeric ``server_version_num`` to ``(major, minor)``.

    Postgres 10+ encodes the version as ``MMMMmm`` (160012 → 16.12).
    Returns ``None`` for any non-int value so the caller can fail
    through to the "couldn't read version" branch without raising.
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return (n // 10000, n % 10000)


def check_pg_connection() -> doctor.CheckResult:
    """Probe the configured Postgres backend (issue #1737, Slice A).

    Runs:

    1. ``SELECT 1`` via the read-only pool (catches "pg not reachable").
    2. ``SHOW server_version_num`` (verifies pg ≥ configured minimum).
    3. ``SELECT 1 FROM pg_extension WHERE extname='vector'`` (verifies
       pgvector is installed — required for embeddings + recall).

    Skipped benignly when ``[storage] backend != "postgres"`` so a
    sqlite-backed install isn't pestered. Failures emit the standard
    three-question doctor message with an actionable fix command.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("pg-connection skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"pg-connection skipped (config parse: {exc})")

    backend = config.storage.backend
    if backend != "postgres":
        return doctor._skip(
            f"pg-connection skipped (backend={backend!r})"
        )

    try:
        from pollypm.storage.pg_pool import get_ro_pool, resolve_dsn
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            "pg pool module unavailable",
            why=(
                "The pollypm pg pool module failed to import — "
                "psycopg/psycopg_pool are missing from this install. "
                "They are required base dependencies (#1813); something "
                "stripped them out of the environment."
            ),
            fix=(
                "Reinstall pollypm so the pg base deps are restored —\n"
                "  uv tool install --reinstall --force pollypm\n"
                "Recheck: pm doctor"
            ),
            data={"error": str(exc)},
        )

    dsn = resolve_dsn(config)

    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            f"pg not reachable at {dsn}",
            why=(
                "PollyPM could not open a read-only connection to "
                "Postgres. The cockpit refuses to start while the "
                "configured backend is unreachable."
            ),
            fix=(
                "Verify pg is running and the DSN is correct —\n"
                "  brew services start postgresql@17\n"
                "  pg_isready\n"
                f"  psql '{dsn}' -c 'SELECT 1'\n"
                "Override the DSN with POLLYPM_PG_DSN if needed.\n"
                "Recheck: pm doctor"
            ),
            data={"dsn": dsn, "error": str(exc)},
        )

    server_version: tuple[int, int] | None = None
    vector_installed = False
    select_ok = False
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            select_ok = bool(row and int(row[0]) == 1)
            cur.execute("SHOW server_version_num")
            row = cur.fetchone()
            server_version = _parse_pg_server_version(row[0]) if row else None
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
            vector_installed = cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        return doctor._fail(
            f"pg probe failed at {dsn}",
            why=(
                "Acquired a connection but the SELECT 1 probe raised. "
                "The pg instance is reachable but not usable for "
                "PollyPM — most likely an auth, schema, or role issue."
            ),
            fix=(
                f"Investigate with psql '{dsn}'.\n"
                "Recheck: pm doctor"
            ),
            data={"dsn": dsn, "error": str(exc)},
        )

    if not select_ok:
        return doctor._fail(
            f"pg probe returned unexpected payload at {dsn}",
            why="SELECT 1 returned something other than 1.",
            fix=(
                f"Investigate with psql '{dsn}'.\n"
                "Recheck: pm doctor"
            ),
            data={"dsn": dsn},
        )

    # Parse the configured minimum version (default "16.0"). Tolerant
    # of "16", "16.0", "16.2.3"; falls back to (16, 0) on a malformed
    # string so a typo can't trivially pass the gate.
    min_version_raw = config.storage.pg.min_version or "16.0"
    parts = [p for p in min_version_raw.split(".") if p.isdigit()]
    if parts:
        min_major = int(parts[0])
        min_minor = int(parts[1]) if len(parts) > 1 else 0
    else:
        min_major, min_minor = 16, 0
    min_version = (min_major, min_minor)

    if server_version is None:
        return doctor._fail(
            f"pg version unreadable at {dsn}",
            why=(
                "The SHOW server_version_num probe returned a value "
                "that didn't parse as an integer."
            ),
            fix=(
                f"Investigate with psql '{dsn}'.\n"
                "Recheck: pm doctor"
            ),
            data={"dsn": dsn},
        )

    if server_version < min_version:
        version_str = f"{server_version[0]}.{server_version[1]}"
        min_str = f"{min_version[0]}.{min_version[1]}"
        return doctor._fail(
            f"pg version {version_str} < required {min_str}",
            why=(
                "PollyPM requires pg "
                f"{min_str}+ for pgvector HNSW indexes and the "
                "generated-column tsvector path. Older pg instances "
                "will silently degrade or fail at migration time."
            ),
            fix=(
                "Upgrade pg —\n"
                "  brew install postgresql@17\n"
                "  brew services start postgresql@17\n"
                "  createdb pollypm\n"
                "Recheck: pm doctor"
            ),
            data={
                "dsn": dsn,
                "server_version": version_str,
                "min_version": min_str,
            },
        )

    if not vector_installed:
        return doctor._fail(
            f"vector extension missing at {dsn}",
            why=(
                "The pgvector extension is required for the "
                "embeddings table + recall path. Without it the schema "
                "migrations will fail and pgvector-backed memory "
                "recall is unavailable."
            ),
            fix=(
                "Install the extension —\n"
                "  brew install pgvector  # or apt install postgresql-NN-pgvector\n"
                f'  psql "{dsn}" -c "CREATE EXTENSION vector"\n'
                "Recheck: pm doctor"
            ),
            data={"dsn": dsn},
        )

    version_str = f"{server_version[0]}.{server_version[1]}"
    return doctor._ok(
        f"pg {version_str} reachable at {dsn} (vector extension installed)",
        data={
            "dsn": dsn,
            "server_version": version_str,
            "vector_installed": True,
        },
    )


# Default freshness threshold for the pg backup probe (issue #1737,
# Slice G). 7 days is a balance: short enough to surface an operator
# who's stopped running ``pm storage backup``; long enough to avoid
# nagging an operator who skipped one weekly run.
PG_BACKUP_STALE_DAYS = 7


def check_pg_backup_freshness() -> doctor.CheckResult:
    """Warn when the most recent pg snapshot is older than N days.

    Skipped on sqlite installs (the sqlite snapshot path has its own
    retention semantics and this check would be noise). Returns a
    warning, not a failure, so a stale backup doesn't gate the cockpit
    from starting — the cutover flow in #1737 §9 surfaces the same
    information through ``pm storage backup --verify`` instead.
    """
    from pollypm.backup import latest_pg_backup_age_seconds
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    if not DEFAULT_CONFIG_PATH.exists():
        return doctor._skip("pg-backup-freshness skipped (no config)")
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return doctor._skip(f"pg-backup-freshness skipped (config parse: {exc})")

    if config.storage.backend != "postgres":
        return doctor._skip(
            f"pg-backup-freshness skipped (backend={config.storage.backend!r})"
        )

    age = latest_pg_backup_age_seconds(config.project.base_dir)
    if age is None:
        return doctor._fail(
            "no pg backups found",
            why=(
                "PollyPM is configured for postgres but no pg_dump "
                "snapshot has been taken yet. The migration runbook "
                "requires a verified backup before any cutover."
            ),
            fix=(
                "Take a backup now —\n"
                "  pm storage backup --verify\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"backup_dir": str(config.project.base_dir / "backups")},
        )
    days = age / 86400.0
    if days > PG_BACKUP_STALE_DAYS:
        return doctor._fail(
            f"latest pg backup is {days:.1f} days old (> {PG_BACKUP_STALE_DAYS}d)",
            why=(
                "Operator backups are the rollback path for the pg "
                "migration. A stale backup means a longer RPO than the "
                "runbook promises."
            ),
            fix=(
                "Refresh the snapshot —\n"
                "  pm storage backup --verify\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"age_days": round(days, 1), "threshold_days": PG_BACKUP_STALE_DAYS},
        )
    return doctor._ok(
        f"latest pg backup is {days:.1f} days old",
        data={"age_days": round(days, 1), "threshold_days": PG_BACKUP_STALE_DAYS},
    )
