"""Install-state checks extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

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
    return doctor._ok(
        f"{len(accounts)} provider account(s) configured",
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
