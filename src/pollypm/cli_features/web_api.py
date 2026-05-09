"""CLI commands for the Web API server (#1547).

Registers two surfaces on the root ``pm`` Typer app:

- ``pm serve [--port N] [--host H] [--allow-remote]`` — run the
  FastAPI app from :mod:`pollypm.web_api`.
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
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger(__name__)


_SERVE_HELP = help_with_examples(
    "Run the PollyPM Web API server (FastAPI) as a peer to the cockpit.",
    [
        ("pm serve", "bind to 127.0.0.1:8765 (default)"),
        ("pm serve --port 9000", "bind to 127.0.0.1:9000"),
        (
            "pm serve --allow-remote --host 0.0.0.0",
            "expose to non-loopback (operator must terminate TLS upstream)",
        ),
    ],
    trailing=(
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
    """
    label = "generated" if generated else "rotated"
    typer.echo(
        f"\n[pm serve] Bearer token {label}.\n"
        f"          Stored at ~/.pollypm/api-token (mode 0600).\n"
        f"          Token: {token}\n",
        err=True,
    )


def register_web_api_commands(app: typer.Typer) -> None:
    """Mount the ``pm serve`` and ``pm api`` commands on the root app."""

    @app.command(name="serve", help=_SERVE_HELP)
    def serve_command(
        port: int = typer.Option(8765, "--port", "-p", help="TCP port to bind."),
        host: str = typer.Option(
            "127.0.0.1",
            "--host",
            help="Bind address. Defaults to 127.0.0.1 (loopback).",
        ),
        allow_remote: bool = typer.Option(
            False,
            "--allow-remote",
            help=(
                "Permit binding to a non-loopback address. The operator is "
                "responsible for terminating TLS upstream (see spec §3)."
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

        if not allow_remote and host not in {"127.0.0.1", "localhost", "::1"}:
            typer.echo(
                f"Error: refusing to bind {host}; pass --allow-remote to enable "
                f"non-loopback binds (spec §3).",
                err=True,
            )
            raise typer.Exit(code=2)

        config = load_config(config_path)
        token, generated = ensure_token(token_path)
        _print_token_once(token, generated=generated)

        app_instance = create_app(config=config, token_path=token_path)

        try:
            import uvicorn
        except ImportError as exc:  # noqa: BLE001
            typer.echo(
                f"Error: uvicorn is required to run `pm serve` ({exc}). "
                f"Install with `pip install pollypm[server]` or `uv sync`.",
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


__all__ = ["register_web_api_commands"]
