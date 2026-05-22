"""``pm chat`` CLI group — HTTP client for the chat endpoints.

Contract:
- Inputs: Typer arguments/options for the three subcommands
  (``list`` / ``history <session>`` / ``send <session> <text>``).
- Outputs: ``chat_app`` Typer mounted under the root ``pm`` app.
- Side effects: HTTPS requests to ``http://127.0.0.1:8765/api/v1/chat/``
  via :mod:`httpx`, reading the bearer token from
  ``~/.pollypm/api-token`` (same auth model as the rest of the Web API
  surface and ``pm sessions``). Read-only for ``list``/``history``;
  ``send`` POSTs and triggers tmux input on the daemon side.
- Invariants: this is Sam's user-test harness for the phase-1 chat
  HTTP surface (`docs/pollypm-chat-endpoints-spec.md` §6 / PR P4).
  Pure HTTP client — no business logic, no transcript parsing — so a
  contract drift between client + server surfaces as the API's own
  error payload, not as a CLI crash.

Errors:
- Network failures and 4xx/5xx responses print the JSON error body
  verbatim (or the raw text if the body isn't JSON) on stderr and
  exit non-zero — the API is the source of truth for error shapes,
  and surfacing them verbatim keeps this client thin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import typer

from pollypm.cli_help import help_with_examples
from pollypm.web_api.token import DEFAULT_TOKEN_PATH


# Default to the same address ``pm serve`` binds to by default. The
# ``POLLYPM_API_BASE`` env var lets a developer redirect the client at
# a non-default port without touching the CLI surface — useful when
# running ``pm serve --port 9000`` alongside the production daemon.
_DEFAULT_BASE_URL = "http://127.0.0.1:8765"
_BASE_URL_ENV = "POLLYPM_API_BASE"

# Network timeout — generous to accommodate a JSONL-parse on a long
# transcript, tight enough that a hung daemon surfaces as a CLI error
# rather than blocking the operator's terminal indefinitely.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


chat_app = typer.Typer(
    help=help_with_examples(
        "HTTP client for the chat endpoints (operator/architect/advisor/worker).",
        [
            ("pm chat list", "list every chat surface the daemon knows about"),
            (
                "pm chat history operator --limit 20",
                "tail the 20 most-recent operator messages",
            ),
            (
                'pm chat send architect_samblog "summarise the queue"',
                "push a message into the Archie pane via tmux",
            ),
        ],
        trailing=(
            "Auth: reads the bearer token from ~/.pollypm/api-token "
            "(same as `pm serve`). Base URL defaults to "
            "http://127.0.0.1:8765 and can be overridden via the "
            "POLLYPM_API_BASE environment variable. On 4xx/5xx the "
            "JSON error body is printed verbatim and the command exits "
            "non-zero."
        ),
    )
)


# ---------------------------------------------------------------------------
# Auth + base URL helpers.
# ---------------------------------------------------------------------------


def _resolve_base_url() -> str:
    """Return the base URL for chat API calls.

    Honors ``POLLYPM_API_BASE`` so a developer can redirect the client
    without rebuilding. The returned value has no trailing slash so
    callers can safely concatenate ``/api/v1/chat/...`` paths.
    """
    raw = os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL
    return raw.rstrip("/")


def _resolve_token_path() -> Path:
    """Return the path to the bearer token file.

    Re-resolved on every call (via :data:`DEFAULT_TOKEN_PATH`) so
    monkeypatching the config home in tests is honored without
    re-importing this module.
    """
    return Path(DEFAULT_TOKEN_PATH)


def _load_token_or_die() -> str:
    """Return the bearer token, exiting non-zero if it isn't present.

    A missing token is a setup problem (the daemon hasn't been started
    yet or the file was deleted) — we surface the absolute path so the
    operator knows exactly where to look, then exit ``2`` to match the
    ``BadParameter`` exit code used elsewhere in the CLI.
    """
    path = _resolve_token_path()
    try:
        if not path.exists():
            typer.echo(
                f"error: bearer token not found at {path}; run `pm serve` "
                "once or `pm api regen-token` to provision.",
                err=True,
            )
            raise typer.Exit(code=2)
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        typer.echo(f"error: could not read token file {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not token:
        typer.echo(
            f"error: token file {path} is empty; run `pm api regen-token`.",
            err=True,
        )
        raise typer.Exit(code=2)
    return token


# ---------------------------------------------------------------------------
# HTTP plumbing.
# ---------------------------------------------------------------------------


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _emit_error_and_exit(response: httpx.Response) -> None:
    """Print the API's error body verbatim and exit non-zero.

    Per the spec, the server returns a JSON ``{code, message, ...}``
    envelope for failures. We pretty-print it (2-space indent) so the
    operator can read the ``code`` directly. If the body isn't JSON
    (rare — likely a proxy or middleware fault), fall back to the raw
    text so we never swallow the cause.
    """
    typer.echo(f"HTTP {response.status_code}", err=True)
    body = response.text
    try:
        payload = response.json()
        typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
    except (ValueError, json.JSONDecodeError):
        if body:
            typer.echo(body, err=True)
    raise typer.Exit(code=1)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue an authenticated request and return the decoded JSON body.

    On any non-2xx response, prints the error body verbatim and exits.
    On a network failure (connection refused, DNS error, timeout),
    surfaces a one-line readable error referencing the base URL so the
    operator knows whether to start ``pm serve``.
    """
    token = _load_token_or_die()
    base = _resolve_base_url()
    url = f"{base}{path}"
    try:
        response = httpx.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=_auth_headers(token),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.RequestError as exc:
        typer.echo(
            f"error: could not reach {url}: {exc.__class__.__name__}: {exc}",
            err=True,
        )
        typer.echo(
            "hint: is `pm serve` running? "
            f"(override base URL with {_BASE_URL_ENV}=...)",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    if response.status_code >= 400:
        _emit_error_and_exit(response)

    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        typer.echo(
            f"error: response from {url} was not JSON: {exc}", err=True
        )
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# `pm chat list`
# ---------------------------------------------------------------------------


def _format_sessions_table(sessions: list[dict[str, Any]]) -> list[str]:
    """Render the list-sessions response as a fixed-column text table.

    Column widths are picked to keep ``session_name`` (which can be a
    long ``task-<project>-<N>`` slug) intact while keeping the row
    under a typical 120-column terminal. ``window`` collapses to ``y``
    or ``n`` because the path itself is rarely useful at a glance — use
    ``--json`` for the full window envelope.
    """
    lines = [
        f"{'SESSION_NAME':<28} "
        f"{'SURFACE':<10} "
        f"{'PERSONA':<10} "
        f"{'PROJECT':<14} "
        f"{'WINDOW'}"
    ]
    for entry in sessions:
        session_name = str(entry.get("session_name") or "?")
        surface = str(entry.get("surface_type") or "?")
        persona = entry.get("persona")
        project = entry.get("project")
        window = entry.get("window") or {}
        present = bool(window.get("present")) if isinstance(window, dict) else False
        lines.append(
            f"{session_name:<28} "
            f"{surface:<10} "
            f"{(persona or '-'):<10} "
            f"{(project or '-'):<14} "
            f"{'y' if present else 'n'}"
        )
    return lines


@chat_app.command(
    "list",
    help=(
        "List every chat surface the daemon knows about. Combines "
        "configured sessions (operator/architect/advisor) with live "
        "worker windows. Use --json for the raw response envelope."
    ),
)
def chat_list(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the raw JSON response body."
    ),
) -> None:
    payload = _request("GET", "/api/v1/chat/sessions")
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    sessions = payload.get("sessions") or []
    if not isinstance(sessions, list):
        typer.echo("error: malformed response: `sessions` is not a list", err=True)
        raise typer.Exit(code=1)
    if not sessions:
        typer.echo("(no chat surfaces registered)")
        return
    for line in _format_sessions_table(sessions):
        typer.echo(line)


# ---------------------------------------------------------------------------
# `pm chat history <session_name>`
# ---------------------------------------------------------------------------


def _format_message_line(message: dict[str, Any]) -> str:
    """Render one MessageEnvelope as a single human-readable line.

    Layout: ``<ts>  <actor>  [<type>]  <text>``. ``text`` is left
    intact (no truncation) so an operator running ``pm chat history``
    sees the same content as the JSON consumer — the API is responsible
    for keeping ``text`` display-ready.
    """
    ts = str(message.get("ts") or "?")
    actor = str(message.get("actor") or message.get("role") or "?")
    type_tag = str(message.get("type") or "?")
    text = str(message.get("text") or "")
    # Strip embedded newlines so each envelope stays on one line; the
    # API's `text` field is already a display-ready summary, and a
    # multi-line render here would break the at-a-glance scrollback
    # the spec's user-test protocol depends on.
    text = text.replace("\n", " ").replace("\r", " ")
    return f"{ts}  {actor:<10}  [{type_tag}]  {text}"


@chat_app.command(
    "history",
    help=(
        "Pull message history for a chat surface. Filters narrow before "
        "rendering. Use --json for the raw MessageEnvelope array."
    ),
)
def chat_history(
    session_name: str = typer.Argument(
        ..., help="Canonical session name (e.g. operator, architect_samblog, task-samblog-47)."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Cap messages returned (server default is 100, max 500).",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="ISO-8601 lower bound on message timestamp.",
    ),
    direction: str | None = typer.Option(
        None,
        "--direction",
        help="`desc` (newest first; server default) or `asc`.",
    ),
    include_subagents: bool = typer.Option(
        False,
        "--include-subagents",
        help="Inline subagent transcripts inside their parent message.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="`auto` (default), `jsonl`, or `capture`.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the raw JSON response body."
    ),
) -> None:
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if since:
        params["since"] = since
    if direction:
        params["direction"] = direction
    if include_subagents:
        # FastAPI bool query params accept the literal string "true".
        params["include_subagents"] = "true"
    if source:
        params["source"] = source

    payload = _request(
        "GET",
        f"/api/v1/chat/{session_name}/messages",
        params=params or None,
    )

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        typer.echo(
            "error: malformed response: `messages` is not a list", err=True
        )
        raise typer.Exit(code=1)
    if not messages:
        typer.echo(f"(no messages for {session_name})")
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        typer.echo(_format_message_line(message))


# ---------------------------------------------------------------------------
# `pm chat send <session_name> <text>`
# ---------------------------------------------------------------------------


@chat_app.command(
    "send",
    help=(
        "Send a message into a chat surface. The daemon routes it through "
        "tmux into the running CLI agent. Use --safety / --answer-to / "
        "--selection for AskUserQuestion replies and mid-stream sends."
    ),
)
def chat_send(
    session_name: str = typer.Argument(
        ..., help="Canonical session name (see `pm chat list`)."
    ),
    text: str = typer.Argument(
        ...,
        help=(
            "Message body. Pass an empty string when answering an "
            "AskUserQuestion via --selection only."
        ),
    ),
    no_enter: bool = typer.Option(
        False,
        "--no-enter",
        help=(
            "Don't press Enter after sending; equivalent to "
            "press_enter=false in the API body."
        ),
    ),
    safety: str | None = typer.Option(
        None,
        "--safety",
        help="strict (default) | loose | force. See spec §4.3.",
    ),
    answer_to: str | None = typer.Option(
        None,
        "--answer-to",
        help="Message id this send is answering (for AskUserQuestion replies).",
    ),
    selection: list[str] = typer.Option(
        [],
        "--selection",
        help=(
            "Option label for an AskUserQuestion reply. Repeatable for "
            "multi-select. Mutually exclusive with free-text per the API."
        ),
    ),
    notes: str | None = typer.Option(
        None,
        "--notes",
        help="Free-text addition appended after --selection values.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the raw JSON response body."
    ),
) -> None:
    body: dict[str, Any] = {
        "text": text,
        "press_enter": not no_enter,
    }
    if safety:
        body["safety"] = safety
    if answer_to:
        body["answer_to"] = answer_to
    if selection:
        # Typer multi-option defaults to ``[]`` when omitted — only emit
        # the field when the operator actually passed at least one.
        body["selections"] = list(selection)
    if notes is not None:
        body["notes"] = notes

    payload = _request(
        "POST",
        f"/api/v1/chat/{session_name}/send",
        json_body=body,
    )

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    # Pretty-print the response. The spec defines the exact fields, so
    # render the ones an operator cares about per-line and drop the
    # full envelope at the bottom of the block via JSON for fidelity.
    ok = payload.get("ok")
    message_id = payload.get("message_id") or "-"
    target = payload.get("window_target") or "-"
    method = payload.get("method") or "-"
    chars = payload.get("characters_sent")
    pressed_at = payload.get("press_enter_at") or "-"

    typer.echo(f"ok:               {ok}")
    typer.echo(f"message_id:       {message_id}")
    typer.echo(f"session_name:     {payload.get('session_name') or session_name}")
    typer.echo(f"window_target:    {target}")
    typer.echo(f"characters_sent:  {chars if chars is not None else '-'}")
    typer.echo(f"method:           {method}")
    typer.echo(f"press_enter_at:   {pressed_at}")


__all__ = [
    "chat_app",
    "chat_list",
    "chat_history",
    "chat_send",
]
