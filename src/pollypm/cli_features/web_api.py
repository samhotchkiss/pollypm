"""CLI commands for the Web API server (#1547).

Registers two surfaces on the root ``pm`` Typer app:

- ``pm serve [--port N] [--host H] [--allow-remote] [--tailscale]`` —
  run the FastAPI app from :mod:`pollypm.web_api`. Default ``pm serve``
  auto-detects Tailscale: if ``tailscale ip -4`` returns an IPv4 the
  daemon binds that tailnet interface (tailnet-trust mode); otherwise
  it falls back to ``127.0.0.1`` (loopback mode). ``--tailscale`` is a
  no-op back-compat flag — auto-detection covers the same path. To
  force loopback even when Tailscale is running, pass
  ``--host 127.0.0.1`` explicitly; that override stays in untrusted
  mode (bearer/cookie required for every request).
- ``pm api regen-token`` — rotate the bearer token. Lives under a
  dedicated ``pm api`` sub-app so future admin commands (``pm api
  show-token``, ``pm api status``) can land beside it without
  cluttering the root command surface.

Both commands compose against :mod:`pollypm.web_api.token` for token
storage and :mod:`pollypm.web_api.app.create_app` for the HTTP
surface.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger(__name__)


def detect_tailscale_ip() -> str | None:
    """Return the operator's Tailscale IPv4 or ``None`` if unavailable.

    Shells out to ``tailscale ip -4`` (the same command the spec uses
    in its access doc). Returns ``None`` for any of:

    - ``tailscale`` binary not on ``$PATH``
    - command exits non-zero (not logged in, daemon down, no IP yet)
    - stdout is empty / not a parseable IPv4

    Never raises — the caller decides whether to fall back to localhost
    with a warning or hard-fail.
    """
    binary = shutil.which("tailscale")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # ``tailscale ip -4`` prints one address per line; take the first
    # non-empty token. We don't validate the dotted-quad shape here —
    # the auth layer's ``is_tailscale_ip`` does that defensively.
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


_SERVE_HELP = help_with_examples(
    "Run the PollyPM Web API server (FastAPI) as a peer to the cockpit.",
    [
        ("pm serve", "auto-detect Tailscale, else bind 127.0.0.1:8765"),
        ("pm serve --port 9000", "same as above on port 9000"),
        (
            "pm serve --tailscale",
            "no-op (kept for back-compat) — auto-detection is on by default",
        ),
    ],
    trailing=(
        "By default ``pm serve`` tries ``tailscale ip -4``. If it "
        "returns an IPv4 the server binds that interface only; "
        "otherwise it falls back to 127.0.0.1. LAN access is "
        "intentionally unsupported in v0 (use Tailscale). "
        "First run prints the bearer token to stderr; rotate via "
        "`pm api regen-token`. The server reads / writes the same "
        "state.db and audit.jsonl as the cockpit, so it works with "
        "the cockpit down."
    ),
)


_API_HELP = help_with_examples(
    "Web API admin commands (token rotation, status, etc.).",
    [
        ("pm api regen-token", "rotate the bearer token; prints the new value once"),
        (
            "pm api regen-token --token-path /tmp/api-token",
            "rotate a non-default token file (useful for tests / fixtures)",
        ),
    ],
)


api_app = typer.Typer(help=_API_HELP, no_args_is_help=True)


def _print_token_once(token: str, *, generated: bool) -> None:
    """Emit the freshly-issued token to stderr on first launch.

    Stderr (not stdout) so a script piping ``pm serve`` for logs
    doesn't accidentally swallow it. Wrapped in a banner so it's
    obvious the token landed.

    Only called when the token was actually generated (or rotated):
    on subsequent ``pm serve`` runs the token already exists on disk
    at mode 0600 and re-emitting it leaks the value into terminal
    scrollback / log files even though the operator already has it.
    """
    label = "generated" if generated else "rotated"
    typer.echo(
        f"\n[pm serve] Bearer token {label}.\n"
        f"          Stored at ~/.pollypm/api-token (mode 0600).\n"
        f"          Token: {token}\n",
        err=True,
    )


def _print_token_location_only(token_path_hint: str) -> None:
    """Tell the operator where to find an already-existing token.

    On every-startup-after-the-first we don't want to spray the
    token across terminal scrollback. A one-line pointer to the
    file (and the rotation command) is enough to recover from an
    "I lost the value" situation without leaking the secret.
    """
    typer.echo(
        f"[pm serve] Bearer token already provisioned at {token_path_hint}.\n"
        f"          Re-issue with `pm api regen-token` if you've lost it.",
        err=True,
    )


def register_web_api_commands(app: typer.Typer) -> None:
    """Mount the ``pm serve`` and ``pm api`` commands on the root app."""

    @app.command(name="serve", help=_SERVE_HELP)
    def serve_command(
        port: int = typer.Option(8765, "--port", "-p", help="TCP port to bind."),
        host: str | None = typer.Option(
            None,
            "--host",
            help=(
                "Override the bind address. Default: auto-detected "
                "Tailscale IPv4, else 127.0.0.1. Passing this flag "
                "skips Tailscale detection entirely."
            ),
        ),
        allow_remote: bool = typer.Option(
            False,
            "--allow-remote",
            help=(
                "Permit binding to a non-loopback / non-tailnet address. "
                "Only meaningful with an explicit --host override; the "
                "operator is responsible for terminating TLS upstream "
                "(see spec §3)."
            ),
        ),
        tailscale: bool = typer.Option(
            False,
            "--tailscale",
            help=(
                "No-op kept for backwards compatibility. Tailscale "
                "auto-detection runs unconditionally by default; the "
                "server binds to the detected tailnet IPv4 only, or "
                "falls back to 127.0.0.1 if Tailscale isn't installed "
                "or hasn't returned an address."
            ),
        ),
        config_path: Path = typer.Option(
            DEFAULT_CONFIG_PATH,
            "--config",
            "-c",
            help="Path to the PollyPM config file.",
        ),
        token_path: Path | None = typer.Option(
            None,
            "--token-path",
            help="Override the bearer-token file location (defaults to ~/.pollypm/api-token).",
        ),
    ) -> None:
        from pollypm.web_api import create_app, ensure_token

        # Bind selection (spec doc decision a-default):
        #
        # - If the operator passed --host, honor it and skip detection
        #   entirely. They know what they're doing.
        # - Otherwise auto-detect Tailscale unconditionally and bind
        #   the detected tailnet IPv4 ONLY. The kernel routing table
        #   then enforces that packets must arrive on the tailscale0
        #   interface to reach the listening socket — spoofed source
        #   IPs from any other interface are dropped before the app
        #   sees them.
        # - If Tailscale isn't installed / hasn't logged in / hasn't
        #   returned an IPv4, fall back to 127.0.0.1 (loopback only).
        #   LAN access is intentionally unsupported in v0.
        # - The --tailscale flag is now a no-op (the behaviour it
        #   used to gate is the default). Warn if it was passed
        #   without a detected IP so the operator understands why
        #   they don't see a tailnet bind.
        bind_mode: str
        tailscale_ip: str | None = None
        # ``tailnet_trust`` decides whether the auth dependency and the
        # ``/ui/`` cookie gate treat 100.64.0.0/10 peers as credential-
        # free. It MUST stay False unless the server actually bound to
        # a verified Tailscale IPv4; otherwise an explicit
        # ``--host 0.0.0.0 --allow-remote`` deploy would hand out a
        # session to any CGNAT-source peer (some ISPs use RFC 6598).
        # Codex round-2 PR #2065.
        tailnet_trust: bool = False
        if host is not None:
            # Explicit operator override — skip detection. Carry through
            # the existing --allow-remote gate so accidental 0.0.0.0
            # binds still trip the safety check.
            bind_mode = "explicit"
            if not allow_remote and host not in {"127.0.0.1", "localhost", "::1"}:
                typer.echo(
                    f"Error: refusing to bind {host}; pass --allow-remote to "
                    f"enable non-loopback binds (spec §3).",
                    err=True,
                )
                raise typer.Exit(code=2)
            # Explicit override stays untrusted by default. If the
            # operator typed the actual tailnet IPv4, opt back into
            # tailnet trust — that's the same wire path as the
            # auto-detected case. Any other explicit host (loopback,
            # 0.0.0.0, a LAN address) leaves tailnet_trust False.
            detected = detect_tailscale_ip()
            if detected is not None and host == detected:
                tailscale_ip = detected
                tailnet_trust = True
        else:
            tailscale_ip = detect_tailscale_ip()
            if tailscale_ip is not None:
                host = tailscale_ip
                bind_mode = "tailscale"
                tailnet_trust = True
                typer.echo(
                    f"[pm serve] bound {tailscale_ip}:{port} (tailscale "
                    f"mode; UI at http://{tailscale_ip}:{port}/ui/)",
                    err=True,
                )
            else:
                host = "127.0.0.1"
                bind_mode = "loopback"
                typer.echo(
                    f"[pm serve] bound 127.0.0.1:{port} (loopback only — "
                    f"install/start Tailscale for network access)",
                    err=True,
                )

        if tailscale and tailscale_ip is None and bind_mode != "tailscale":
            typer.echo(
                "Warning: --tailscale passed but `tailscale ip -4` "
                "returned no IP (binary missing, not logged in, or no "
                "IPv4 yet). Falling back to loopback-only.",
                err=True,
            )

        config = load_config(config_path)
        token, generated = ensure_token(token_path)
        if generated:
            _print_token_once(token, generated=generated)
        else:
            from pollypm.web_api.token import DEFAULT_TOKEN_PATH

            _print_token_location_only(
                str(token_path) if token_path is not None else str(DEFAULT_TOKEN_PATH)
            )

        app_instance = create_app(
            config=config,
            token_path=token_path,
            tailnet_trust_enabled=tailnet_trust,
        )

        try:
            import uvicorn
        except ImportError as exc:  # noqa: BLE001
            # uvicorn is a core PollyPM dependency (see pyproject.toml).
            # If the import fails the install is broken — there's no
            # ``[server]`` extra to opt into. The earlier message that
            # pointed at ``pollypm[server]`` was misleading.
            typer.echo(
                f"Error: uvicorn is required to run `pm serve` ({exc}). "
                f"It's a core PollyPM dependency — try `uv sync` or "
                f"`pip install --force-reinstall pollypm` to repair the env.",
                err=True,
            )
            raise typer.Exit(code=1) from exc

        typer.echo(f"[pm serve] http://{host}:{port}/api/v1/", err=True)
        uvicorn.run(app_instance, host=host, port=port, log_level="info")

    @api_app.command(name="regen-token", help="Rotate the API bearer token.")
    def regen_token_command(
        token_path: Path | None = typer.Option(
            None,
            "--token-path",
            help="Override the bearer-token file location (defaults to ~/.pollypm/api-token).",
        ),
    ) -> None:
        from pollypm.web_api import regenerate_token

        token = regenerate_token(token_path)
        typer.echo(
            "Bearer token rotated. The new token is shown ONCE — copy it now:",
            err=True,
        )
        # Print to stdout so a script can capture it (`pm api regen-token > token`).
        typer.echo(token)

    app.add_typer(api_app, name="api", help=_API_HELP)


__all__ = ["detect_tailscale_ip", "register_web_api_commands"]
