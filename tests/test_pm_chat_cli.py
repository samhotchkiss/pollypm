"""Tests for ``pm chat`` — HTTP client CLI for chat endpoints (PR P4).

The CLI is a thin wrapper around the chat HTTP surface defined in
``docs/pollypm-chat-endpoints-spec.md``. Tests run via Typer's
``CliRunner`` with ``httpx.request`` monkey-patched so we exercise
argument parsing, request shaping, and response rendering without
needing a live ``pm serve`` daemon.

Token resolution is redirected at a tmp file per-test so we cover the
"token present" / "token missing" branches without touching
``~/.pollypm``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

from pollypm.cli_features import chat as chat_mod


runner = CliRunner()


# ---------------------------------------------------------------------------
# Test scaffolding — fake httpx + tmp token.
# ---------------------------------------------------------------------------


@dataclass
class _Capture:
    """Records what the fake httpx adapter saw."""

    method: str = ""
    url: str = ""
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _build_cli_app() -> typer.Typer:
    """Mount ``chat_app`` under a parent Typer so subcommand dispatch
    works the same as the production ``pollypm.cli.app``.
    """
    app = typer.Typer()
    app.add_typer(chat_mod.chat_app, name="chat")

    @app.command("_marker")
    def _marker() -> None:  # pragma: no cover - presence-only
        return None

    return app


def _install_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Write a fake token file and redirect the resolver at it.

    Returns the token path so tests can also delete/blank it to drive
    the missing-token branches.
    """
    token_path = tmp_path / "api-token"
    token_path.write_text("test-token-abcdef", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "DEFAULT_TOKEN_PATH", token_path)
    return token_path


def _install_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    json_payload: Any = None,
    text_payload: str | None = None,
) -> _Capture:
    """Stub ``httpx.request`` to return a canned response and capture
    the outgoing request shape for assertions.
    """
    capture = _Capture()

    def _fake_request(
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,  # noqa: A002 - matches httpx signature
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> httpx.Response:
        capture.method = method
        capture.url = url
        capture.params = dict(params) if params else None
        capture.json_body = dict(json) if json else None
        capture.headers = dict(headers or {})
        if json_payload is not None:
            import json as _json_mod
            body = _json_mod.dumps(json_payload).encode("utf-8")
            request = httpx.Request(method, url)
            return httpx.Response(
                status_code,
                content=body,
                headers={"content-type": "application/json"},
                request=request,
            )
        request = httpx.Request(method, url)
        return httpx.Response(
            status_code,
            content=(text_payload or "").encode("utf-8"),
            request=request,
        )

    monkeypatch.setattr(chat_mod.httpx, "request", _fake_request)
    return capture


# ---------------------------------------------------------------------------
# `pm chat list`
# ---------------------------------------------------------------------------


class TestChatList:
    def test_happy_path_renders_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(
            monkeypatch,
            json_payload={
                "sessions": [
                    {
                        "session_name": "operator",
                        "surface_type": "operator",
                        "persona": "Polly",
                        "project": None,
                        "window": {"present": True},
                    },
                    {
                        "session_name": "architect_samblog",
                        "surface_type": "architect",
                        "persona": "Archie",
                        "project": "samblog",
                        "window": {"present": False},
                    },
                ]
            },
        )

        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code == 0, result.output
        assert "SESSION_NAME" in result.output
        assert "operator" in result.output
        assert "architect_samblog" in result.output
        assert "Polly" in result.output
        assert "Archie" in result.output
        # `present=True` → "y"; `present=False` → "n".
        lines = {
            line.split()[0]: line
            for line in result.output.splitlines()
            if line and not line.startswith("SESSION_NAME")
        }
        assert lines["operator"].rstrip().endswith("y")
        assert lines["architect_samblog"].rstrip().endswith("n")
        assert capture.method == "GET"
        assert capture.url.endswith("/api/v1/chat/sessions")
        assert capture.headers.get("Authorization") == "Bearer test-token-abcdef"

    def test_json_flag_emits_raw_envelope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        payload = {
            "sessions": [
                {
                    "session_name": "operator",
                    "surface_type": "operator",
                    "persona": "Polly",
                    "project": None,
                    "window": {"present": True},
                }
            ]
        }
        _install_response(monkeypatch, json_payload=payload)
        result = runner.invoke(_build_cli_app(), ["chat", "list", "--json"])
        assert result.exit_code == 0, result.output
        # Output must round-trip through json.loads — i.e. it's the raw
        # envelope, not the pretty-printed table.
        decoded = json.loads(result.output)
        assert decoded == payload

    def test_empty_sessions_list_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        _install_response(monkeypatch, json_payload={"sessions": []})
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code == 0, result.output
        assert "no chat surfaces" in result.output

    def test_401_unauthorized_prints_error_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        _install_response(
            monkeypatch,
            status_code=401,
            json_payload={
                "code": "invalid_token",
                "message": "bearer token did not match",
            },
        )
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code != 0
        # Error body must appear in mixed output verbatim.
        assert "HTTP 401" in result.output
        assert "invalid_token" in result.output

    def test_network_failure_surfaces_readable_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)

        def _boom(*_args, **_kwargs) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(chat_mod.httpx, "request", _boom)
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code != 0
        assert "could not reach" in result.output
        assert "pm serve" in result.output


# ---------------------------------------------------------------------------
# `pm chat history <session>`
# ---------------------------------------------------------------------------


class TestChatHistory:
    def test_happy_path_renders_message_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(
            monkeypatch,
            json_payload={
                "session_name": "sess1",
                "messages": [
                    {
                        "id": "msg_1",
                        "ts": "2026-05-21T20:53:12.123Z",
                        "role": "user",
                        "actor": "Sam",
                        "type": "text",
                        "text": "hello",
                    },
                    {
                        "id": "msg_2",
                        "ts": "2026-05-21T20:53:15.500Z",
                        "role": "assistant",
                        "actor": "Polly",
                        "type": "text",
                        "text": "hi back",
                    },
                ],
            },
        )

        result = runner.invoke(_build_cli_app(), ["chat", "history", "sess1"])
        assert result.exit_code == 0, result.output
        assert "Sam" in result.output
        assert "Polly" in result.output
        assert "[text]" in result.output
        assert "hello" in result.output
        assert "hi back" in result.output
        assert capture.method == "GET"
        assert capture.url.endswith("/api/v1/chat/sess1/messages")
        # No filters → no query params emitted at all.
        assert capture.params is None

    def test_query_params_forwarded_for_limit_and_since(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(
            monkeypatch, json_payload={"messages": []}
        )
        result = runner.invoke(
            _build_cli_app(),
            [
                "chat",
                "history",
                "sess1",
                "--limit",
                "5",
                "--since",
                "2026-05-21",
            ],
        )
        assert result.exit_code == 0, result.output
        assert capture.params == {"limit": 5, "since": "2026-05-21"}

    def test_include_subagents_sets_true_query_param(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(
            monkeypatch, json_payload={"messages": []}
        )
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "history", "sess1", "--include-subagents"],
        )
        assert result.exit_code == 0, result.output
        assert (capture.params or {}).get("include_subagents") == "true"

    def test_direction_and_source_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(
            monkeypatch, json_payload={"messages": []}
        )
        result = runner.invoke(
            _build_cli_app(),
            [
                "chat",
                "history",
                "sess1",
                "--direction",
                "asc",
                "--source",
                "jsonl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (capture.params or {}).get("direction") == "asc"
        assert (capture.params or {}).get("source") == "jsonl"

    def test_empty_messages_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        _install_response(monkeypatch, json_payload={"messages": []})
        result = runner.invoke(
            _build_cli_app(), ["chat", "history", "sess1"]
        )
        assert result.exit_code == 0, result.output
        assert "no messages for sess1" in result.output

    def test_json_flag_emits_raw_envelope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        payload = {
            "messages": [
                {
                    "id": "msg_1",
                    "ts": "2026-05-21T20:53:12Z",
                    "role": "assistant",
                    "actor": "Polly",
                    "type": "text",
                    "text": "hi",
                }
            ]
        }
        _install_response(monkeypatch, json_payload=payload)
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "history", "sess1", "--json"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == payload

    def test_multiline_text_collapsed_to_single_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        _install_response(
            monkeypatch,
            json_payload={
                "messages": [
                    {
                        "id": "msg_1",
                        "ts": "2026-05-21T20:53:12Z",
                        "role": "user",
                        "actor": "Sam",
                        "type": "text",
                        "text": "line1\nline2\nline3",
                    }
                ]
            },
        )
        result = runner.invoke(
            _build_cli_app(), ["chat", "history", "sess1"]
        )
        assert result.exit_code == 0, result.output
        # Exactly one body line (plus optional trailing newline). The
        # envelope had two embedded newlines — both must be stripped.
        non_empty = [
            line for line in result.output.splitlines() if line.strip()
        ]
        assert len(non_empty) == 1
        assert "line1 line2 line3" in non_empty[0]


# ---------------------------------------------------------------------------
# `pm chat send <session> <text>`
# ---------------------------------------------------------------------------


def _send_payload() -> dict[str, Any]:
    """Canonical 200 response per spec §2.3."""
    return {
        "ok": True,
        "message_id": "msg_abc",
        "session_name": "sess1",
        "window_target": "storage-closet:pm-operator",
        "characters_sent": 5,
        "method": "send_keys",
        "press_enter_at": "2026-05-21T20:53:12.500Z",
    }


class TestChatSend:
    def test_happy_path_posts_text_and_renders_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload=_send_payload())
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "send", "sess1", "hello"],
        )
        assert result.exit_code == 0, result.output
        assert capture.method == "POST"
        assert capture.url.endswith("/api/v1/chat/sess1/send")
        assert capture.json_body == {"text": "hello", "press_enter": True}
        # Pretty-printed response surfaces the fields an operator cares about.
        assert "message_id:" in result.output
        assert "msg_abc" in result.output
        assert "window_target:" in result.output
        assert "storage-closet:pm-operator" in result.output

    def test_no_enter_flag_sets_press_enter_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload=_send_payload())
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "send", "sess1", "hello", "--no-enter"],
        )
        assert result.exit_code == 0, result.output
        assert (capture.json_body or {}).get("press_enter") is False

    def test_selection_and_answer_to_for_ask_user(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload=_send_payload())
        result = runner.invoke(
            _build_cli_app(),
            [
                "chat",
                "send",
                "sess1",
                "",
                "--answer-to",
                "msg_abc",
                "--selection",
                "Option A",
            ],
        )
        assert result.exit_code == 0, result.output
        body = capture.json_body or {}
        assert body.get("text") == ""
        assert body.get("answer_to") == "msg_abc"
        assert body.get("selections") == ["Option A"]

    def test_repeated_selection_for_multi_select(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload=_send_payload())
        result = runner.invoke(
            _build_cli_app(),
            [
                "chat",
                "send",
                "sess1",
                "",
                "--answer-to",
                "msg_abc",
                "--selection",
                "A",
                "--selection",
                "B",
                "--notes",
                "fine either way",
            ],
        )
        assert result.exit_code == 0, result.output
        body = capture.json_body or {}
        assert body.get("selections") == ["A", "B"]
        assert body.get("notes") == "fine either way"

    def test_safety_force_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload=_send_payload())
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "send", "sess1", "x", "--safety", "force"],
        )
        assert result.exit_code == 0, result.output
        assert (capture.json_body or {}).get("safety") == "force"

    def test_409_unsafe_mid_tool_prints_error_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        _install_response(
            monkeypatch,
            status_code=409,
            json_payload={
                "code": "unsafe_mid_tool",
                "message": (
                    "last assistant message has an open tool_use without "
                    "a matching tool_result; pass --safety force to override"
                ),
            },
        )
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "send", "sess1", "hello"],
        )
        assert result.exit_code != 0
        assert "HTTP 409" in result.output
        assert "unsafe_mid_tool" in result.output
        assert "--safety force" in result.output

    def test_send_json_flag_emits_raw_envelope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        payload = _send_payload()
        _install_response(monkeypatch, json_payload=payload)
        result = runner.invoke(
            _build_cli_app(),
            ["chat", "send", "sess1", "hello", "--json"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == payload


# ---------------------------------------------------------------------------
# Token-resolution edge cases.
# ---------------------------------------------------------------------------


class TestTokenResolution:
    def test_missing_token_file_exits_with_readable_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point the resolver at a path that doesn't exist — must not
        # touch real ~/.pollypm.
        missing = tmp_path / "no-such-token"
        monkeypatch.setattr(chat_mod, "DEFAULT_TOKEN_PATH", missing)
        # No httpx stub — if we accidentally tried to make a request,
        # the test would either crash or hit the network. The CLI must
        # short-circuit before reaching httpx.
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code != 0
        assert "bearer token not found" in result.output
        assert str(missing) in result.output

    def test_empty_token_file_exits_with_readable_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "api-token"
        token_path.write_text("   \n", encoding="utf-8")
        monkeypatch.setattr(chat_mod, "DEFAULT_TOKEN_PATH", token_path)
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code != 0
        assert "empty" in result.output


# ---------------------------------------------------------------------------
# Base URL override via env var.
# ---------------------------------------------------------------------------


class TestBaseUrlOverride:
    def test_env_var_overrides_default_base(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_token(monkeypatch, tmp_path)
        capture = _install_response(monkeypatch, json_payload={"sessions": []})
        monkeypatch.setenv("POLLYPM_API_BASE", "http://example.test:9999/")
        result = runner.invoke(_build_cli_app(), ["chat", "list"])
        assert result.exit_code == 0, result.output
        # Trailing slash on the env var must be stripped so the joined
        # URL doesn't end up with a double slash.
        assert capture.url == "http://example.test:9999/api/v1/chat/sessions"
