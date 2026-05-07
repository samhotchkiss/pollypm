"""`pm plugins` subcommand — list, show, install, uninstall, enable, disable, doctor.

See docs/plugin-discovery-spec.md §7 for the surface. Every command
accepts ``--json`` for machine consumption. Commands that mutate the
on-disk plugin set (``install`` / ``uninstall`` / ``enable`` /
``disable``) write to user-global locations — projects are left alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import typer

from pollypm.cli_help import help_with_examples

plugins_app = typer.Typer(
    help=help_with_examples(
        "Manage PollyPM plugins.",
        [
            ("pm plugins list", "show installed and disabled plugins"),
            ("pm plugins show local_heartbeat", "inspect one plugin manifest"),
            ("pm plugins install ~/dev/my-plugin", "install a local plugin"),
        ],
    )
)


USER_PLUGINS_DIR = Path.home() / ".pollypm" / "plugins"
USER_CONFIG_PATH = Path.home() / ".pollypm" / "pollypm.toml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_host():
    """Build a fresh ExtensionHost rooted at cwd, honouring the user's
    plugins.disabled config.

    Reads the disabled list directly from the module-level
    ``USER_CONFIG_PATH`` so test fixtures can rebind it without
    monkey-patching ``config.DEFAULT_CONFIG_PATH`` (which is fixed at
    import time).
    """
    from pollypm.plugin_host import ExtensionHost

    root = Path.cwd()
    disabled = tuple(_current_disabled())
    return ExtensionHost(root, disabled=disabled)


def _capability_to_dict(cap: Any) -> dict[str, Any]:
    return {
        "kind": cap.kind,
        "name": cap.name,
        "replaces": list(cap.replaces),
        "requires_api": cap.requires_api,
    }


def _plugin_summary(host: Any, name: str) -> dict[str, Any]:
    plugins = host.plugins()
    plugin = plugins.get(name)
    source = host.plugin_source(name)
    summary: dict[str, Any] = {
        "name": name,
        "source": source,
        "status": "loaded" if plugin is not None else "disabled",
    }
    if plugin is not None:
        summary["version"] = plugin.version
        summary["description"] = plugin.description
        summary["capabilities"] = [_capability_to_dict(c) for c in plugin.capabilities]
        degraded = host.degraded_plugins.get(name)
        if degraded:
            summary["status"] = "degraded"
            summary["degraded_reason"] = degraded
    else:
        record = host.disabled_plugins.get(name)
        if record is not None:
            summary["status"] = "disabled"
            summary["reason"] = record.reason
            summary["detail"] = record.detail
            summary["source"] = record.source
    return summary


def _read_user_config_toml() -> tuple[dict[str, Any], Path]:
    """Load the user-global pollypm.toml as a raw dict. Returns
    ``({}, path)`` if the file doesn't exist yet.
    """
    if not USER_CONFIG_PATH.exists():
        return {}, USER_CONFIG_PATH
    return tomllib.loads(USER_CONFIG_PATH.read_text()), USER_CONFIG_PATH


def _write_disabled_list(disabled: list[str]) -> None:
    """Update (or create) the ``[plugins].disabled`` list in the user
    config. Preserves every other line of the existing file and only
    rewrites the ``[plugins]`` table.
    """
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = USER_CONFIG_PATH.read_text() if USER_CONFIG_PATH.exists() else ""
    lines = existing.splitlines() if existing else []

    # Strip any existing [plugins] section.
    new_lines: list[str] = []
    in_plugins = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_plugins = stripped == "[plugins]"
            if in_plugins:
                continue
        if in_plugins:
            # Skip until the next section.
            if stripped.startswith("[") and stripped.endswith("]"):
                in_plugins = stripped == "[plugins]"
                if not in_plugins:
                    new_lines.append(line)
            continue
        new_lines.append(line)

    # Trim trailing blank lines.
    while new_lines and not new_lines[-1].strip():
        new_lines.pop()

    rendered = "\n".join(new_lines)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"

    if disabled:
        unique = list(dict.fromkeys(disabled))
        items = ", ".join(f'"{name}"' for name in unique)
        rendered += "\n[plugins]\n"
        rendered += f"disabled = [{items}]\n"
    USER_CONFIG_PATH.write_text(rendered)


def _current_disabled() -> list[str]:
    raw, _ = _read_user_config_toml()
    plugins_block = raw.get("plugins", {})
    if not isinstance(plugins_block, dict):
        return []
    disabled_raw = plugins_block.get("disabled", [])
    if not isinstance(disabled_raw, list):
        return []
    return [str(x) for x in disabled_raw if isinstance(x, str)]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@plugins_app.command("list")
def list_plugins(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List every loaded plugin with its capabilities and source."""
    host = _build_host()
    plugins = host.plugins()
    names = sorted(set(plugins) | set(host.disabled_plugins))
    summaries = [_plugin_summary(host, name) for name in names]

    if output_json:
        typer.echo(json.dumps(summaries, indent=2, default=str))
        return

    if not summaries:
        typer.echo("No plugins loaded.")
        return

    typer.echo(f"{'NAME':<30}{'SOURCE':<14}{'VERSION':<10}STATUS / CAPABILITIES")
    for item in summaries:
        caps = item.get("capabilities") or []
        if caps:
            cap_str = ", ".join(f"{c['kind']}:{c['name']}" for c in caps)
        elif item.get("status") in {"disabled", "degraded"}:
            reason = item.get("reason") or item.get("degraded_reason", "")
            cap_str = f"{item['status']} — {reason}"
        else:
            cap_str = item["status"]
        typer.echo(
            f"{item['name']:<30}{str(item.get('source') or '-'):<14}"
            f"{str(item.get('version') or '-'):<10}{cap_str}"
        )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@plugins_app.command("show")
def show_plugin(
    name: str = typer.Argument(..., help="Plugin name."),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show manifest + resolved content paths + load errors for a plugin."""
    host = _build_host()
    host.plugins()  # force load so content paths/declarations populate
    plugins = host.plugins()
    summary = _plugin_summary(host, name)
    if name not in plugins and name not in host.disabled_plugins:
        if output_json:
            typer.echo(json.dumps({"error": f"plugin '{name}' not found"}))
        else:
            typer.echo(f"Plugin '{name}' not found.")
        raise typer.Exit(code=1)

    content_decl = host.content_declaration(name)
    if content_decl is not None:
        summary["content"] = {
            "kinds": list(content_decl.kinds),
            "user_paths": list(content_decl.user_paths),
            "resolved": [str(p) for p in host.content_paths(name)],
        }
    else:
        summary["content"] = None

    related_errors = [e for e in host.errors if name in e]
    summary["errors"] = related_errors

    if output_json:
        typer.echo(json.dumps(summary, indent=2, default=str))
        return

    typer.echo(f"Plugin: {summary['name']}")
    typer.echo(f"  Source: {summary.get('source')}")
    typer.echo(f"  Status: {summary['status']}")
    if summary.get("version"):
        typer.echo(f"  Version: {summary['version']}")
    if summary.get("description"):
        typer.echo(f"  Description: {summary['description']}")
    caps = summary.get("capabilities") or []
    if caps:
        typer.echo("  Capabilities:")
        for cap in caps:
            suffix = ""
            if cap["replaces"]:
                suffix += f" replaces={cap['replaces']}"
            if cap["requires_api"]:
                suffix += f" requires_api={cap['requires_api']}"
            typer.echo(f"    - {cap['kind']}:{cap['name']}{suffix}")
    if summary["content"]:
        typer.echo("  Content:")
        typer.echo(f"    kinds: {summary['content']['kinds']}")
        typer.echo(f"    user_paths: {summary['content']['user_paths']}")
        typer.echo("    resolved:")
        for path in summary["content"]["resolved"]:
            typer.echo(f"      - {path}")
    if summary.get("errors"):
        typer.echo("  Errors:")
        for err in summary["errors"]:
            typer.echo(f"    - {err}")
    if summary.get("reason"):
        typer.echo(f"  Disabled reason: {summary['reason']}")
    if summary.get("degraded_reason"):
        typer.echo(f"  Degraded reason: {summary['degraded_reason']}")


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


def _looks_like_git_url(spec: str) -> bool:
    return (
        spec.startswith("git+")
        or spec.startswith("https://")
        or spec.startswith("http://")
        or spec.startswith("git@")
        or spec.endswith(".git")
    )


@plugins_app.command("install")
def install_plugin(
    spec: str = typer.Argument(..., help="pip spec, git URL, or local directory."),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Install a plugin.

    * Local directory — copied into ``~/.pollypm/plugins/<name>/``.
    * Git URL — cloned into ``~/.pollypm/plugins/<basename>/``.
    * Anything else — shelled to ``pip install <spec>`` (for
      entry-point plugins registered under ``pollypm.plugins``).

    Plugins run in-process; treat them like any other dependency.
    """
    USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"spec": spec}

    spec_path = Path(spec).expanduser()
    if spec_path.is_dir():
        target = USER_PLUGINS_DIR / spec_path.name
        if target.exists():
            result["error"] = f"Target {target} already exists"
            code = 1
        else:
            shutil.copytree(spec_path, target)
            result["installed"] = str(target)
            result["method"] = "directory_copy"
            code = 0
    elif _looks_like_git_url(spec):
        basename = Path(spec.rstrip("/").removesuffix(".git")).name
        if spec.startswith("git+"):
            basename = Path(spec.removeprefix("git+").rstrip("/").removesuffix(".git")).name
        target = USER_PLUGINS_DIR / basename
        if target.exists():
            result["error"] = f"Target {target} already exists"
            code = 1
        else:
            try:
                # ``--`` stops git from interpreting a hostile ``spec`` as a
                # flag (e.g. ``--upload-pack=cmd``). ``_looks_like_git_url``
                # already gates the prefixes, but defense in depth is cheap.
                subprocess.run(
                    ["git", "clone", "--", spec, str(target)],
                    check=True, capture_output=True, text=True,
                )
                result["installed"] = str(target)
                result["method"] = "git_clone"
                code = 0
            except subprocess.CalledProcessError as exc:
                result["error"] = f"git clone failed: {exc.stderr.strip() or exc}"
                code = 1
    else:
        try:
            # ``--`` separates pip's options from positional package
            # arguments — without it a malicious ``spec`` like
            # ``--index-url=http://attacker.example`` would be parsed as
            # an option rather than a package name.
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--", spec],
                check=True, capture_output=True, text=True,
            )
            result["method"] = "pip_install"
            result["stdout"] = proc.stdout
            code = 0
        except subprocess.CalledProcessError as exc:
            result["error"] = f"pip install failed: {exc.stderr.strip() or exc}"
            code = 1

    if output_json:
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        if result.get("error"):
            typer.echo(f"Install failed: {result['error']}", err=True)
        else:
            typer.echo(f"Installed ({result['method']}) → {result.get('installed', spec)}")
            typer.echo("Restart pm for the plugin to take effect.")
    raise typer.Exit(code=code)


@plugins_app.command("uninstall")
def uninstall_plugin(
    name: str = typer.Argument(..., help="Plugin name."),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Uninstall a plugin.

    Searches ``~/.pollypm/plugins/<name>/`` first. If not present and
    the plugin came from an entry-point package, falls back to
    ``pip uninstall``.
    """
    target = USER_PLUGINS_DIR / name
    result: dict[str, Any] = {"name": name}
    code = 0
    if target.is_dir():
        shutil.rmtree(target)
        result["removed"] = str(target)
        result["method"] = "directory_remove"
    else:
        try:
            # ``--`` for the same reason as the install path: keep ``name``
            # strictly positional even if a future caller forwards a
            # hyphen-leading string.
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", "--", name],
                check=True, capture_output=True, text=True,
            )
            result["method"] = "pip_uninstall"
            result["stdout"] = proc.stdout
        except subprocess.CalledProcessError as exc:
            result["error"] = f"pip uninstall failed: {exc.stderr.strip() or exc}"
            code = 1

    if output_json:
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        if result.get("error"):
            typer.echo(f"Uninstall failed: {result['error']}", err=True)
        else:
            typer.echo(f"Uninstalled ({result['method']}) → {result.get('removed', name)}")
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@plugins_app.command("enable")
def enable_plugin(
    name: str = typer.Argument(..., help="Plugin name."),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Remove a plugin from ``[plugins].disabled`` in the user config."""
    current = _current_disabled()
    if name not in current:
        result = {"name": name, "status": "already_enabled"}
        code = 0
    else:
        current.remove(name)
        _write_disabled_list(current)
        result = {"name": name, "status": "enabled", "disabled": current}
        code = 0
    if output_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        if result["status"] == "already_enabled":
            typer.echo(f"Plugin '{name}' is already enabled.")
        else:
            typer.echo(f"Enabled '{name}'. Restart pm for the change to take effect.")
    raise typer.Exit(code=code)


@plugins_app.command("disable")
def disable_plugin(
    name: str = typer.Argument(..., help="Plugin name."),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a plugin to ``[plugins].disabled`` in the user config."""
    current = _current_disabled()
    if name in current:
        result = {"name": name, "status": "already_disabled"}
        code = 0
    else:
        current.append(name)
        _write_disabled_list(current)
        result = {"name": name, "status": "disabled", "disabled": current}
        code = 0
    if output_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        if result["status"] == "already_disabled":
            typer.echo(f"Plugin '{name}' is already disabled.")
        else:
            typer.echo(f"Disabled '{name}'. Restart pm for the change to take effect.")
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@plugins_app.command("doctor")
def plugin_doctor(
    output_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Validate every manifest, report every warning, and surface overrides.

    ``pm plugins doctor`` is the frontend for every warning that today
    only lives in ``logger.debug`` — plugin validation strict-mode plus
    the override collision log.
    """
    host = _build_host()

    # Force full validation pass — identical to load-time but surface
    # every per-plugin result so operators can see the counts.
    from pollypm.plugin_validate import validate_all_plugins

    report = validate_all_plugins(host)

    doctor: dict[str, Any] = {
        "plugins_loaded": len(host.plugins()),
        "plugins_disabled": len(host.disabled_plugins),
        "plugins_degraded": len(host.degraded_plugins),
        "errors": list(host.errors),
        "disabled": [
            {
                "name": rec.name,
                "source": rec.source,
                "reason": rec.reason,
                "detail": rec.detail,
            }
            for rec in host.disabled_plugins.values()
        ],
        "degraded": [
            {"name": name, "reason": reason}
            for name, reason in host.degraded_plugins.items()
        ],
        "validation": {
            "all_passed": report.all_passed,
            "results": [
                {
                    "name": r.plugin_name,
                    "passed": r.passed,
                    "checks": len(r.checks),
                    "errors": list(r.errors),
                }
                for r in report.results
            ],
        },
    }

    if output_json:
        typer.echo(json.dumps(doctor, indent=2, default=str))
        return

    typer.echo(f"Plugins loaded:   {doctor['plugins_loaded']}")
    typer.echo(f"Plugins disabled: {doctor['plugins_disabled']}")
    typer.echo(f"Plugins degraded: {doctor['plugins_degraded']}")
    typer.echo(f"Validation pass:  {doctor['validation']['all_passed']}")
    if doctor["disabled"]:
        typer.echo("\nDisabled plugins:")
        for item in doctor["disabled"]:
            typer.echo(f"  - {item['name']} [{item['source']}] {item['reason']}: {item['detail']}")
    if doctor["degraded"]:
        typer.echo("\nDegraded plugins:")
        for item in doctor["degraded"]:
            typer.echo(f"  - {item['name']}: {item['reason']}")
    if doctor["errors"]:
        typer.echo("\nErrors:")
        for err in doctor["errors"]:
            typer.echo(f"  - {err}")
    failing = [r for r in doctor["validation"]["results"] if not r["passed"]]
    if failing:
        typer.echo("\nValidation failures:")
        for r in failing:
            typer.echo(f"  - {r['name']}: {r['errors']}")
