"""Unit tests for the chat-API transcript parser (P1 spec §3).

Exercises every MessageEnvelope discriminator the parser produces:

- ``text`` for user_turn / assistant_turn
- ``tool_use`` for generic Claude tool calls (Bash, Read, Edit, Write,
  Glob, Grep, WebFetch, WebSearch, custom)
- ``tool_result`` for matching results
- ``subagent_spawn`` / ``subagent_result`` for Task tool pairs
- ``ask_user`` for AskUserQuestion
- ``file`` for SendUserFile
- ``system_event`` for error / turn_end

Also covers:

- Codex-shape event handling (assistant_message, tool_call, tool_result)
- Stale-archive detection (spec §4.7)
- Malformed JSON line skip
- ``resolve_transcript_path`` cwd matching + most-recent fallback
- Subagent linking via task-notification block
- Session-index fingerprint matching (cwd + account + provider) with
  explicit ``None`` on unresolved surfaces (Codex review #2044)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pollypm.web_api.chat import (
    MessageRole,
    MessageType,
    ParserInternalType,
    STALE_THRESHOLD_SECONDS,
    is_archive_stale,
    parse_events_jsonl,
    parse_events_jsonl_tail,
    resolve_transcript_path,
)
from pollypm.web_api.chat import transcripts as transcripts_module
from pollypm.web_api.chat.transcripts import (
    _extract_text_from_blocks,
    _format_tool_use_summary,
    _looks_like_subagent_result,
    _parse_cache_clear,
    build_session_index,
    lookup_transcript_path,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
    )


def _claude_event(event_type: str, **payload_kwargs) -> dict:
    """Build a Claude-shaped ingestor event for tests."""
    return {
        "timestamp": "2026-05-21T20:48:11Z",
        "event_type": event_type,
        "session_id": "session-abc",
        "account_name": "claude_main",
        "provider": "claude",
        "project_key": "demo",
        "source_path": "/tmp/raw.jsonl",
        "source_offset": 0,
        "cwd": "/tmp/repo",
        "model_name": "claude-opus-4-7",
        "payload": payload_kwargs,
    }


def _codex_event(event_type: str, **payload_kwargs) -> dict:
    return {
        "timestamp": "2026-05-21T21:00:00Z",
        "event_type": event_type,
        "session_id": "codex-xyz",
        "account_name": "codex_main",
        "provider": "codex",
        "project_key": "demo",
        "source_path": "/tmp/rollout.jsonl",
        "source_offset": 0,
        "cwd": "/tmp/repo",
        "model_name": "gpt-5",
        "payload": payload_kwargs,
    }


# ---------------------------------------------------------------------------
# Basic text turns
# ---------------------------------------------------------------------------


def test_parses_claude_user_turn_as_text_envelope(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("user_turn", text="Hello agent")])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.type == MessageType.TEXT
    assert env.role == MessageRole.USER
    assert env.text == "Hello agent"
    assert env.actor == "user"
    assert env.metadata["provider"] == "claude"


def test_parses_claude_assistant_turn_with_actor_fallback(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("assistant_turn", text="Done.")])
    envelopes = parse_events_jsonl(events_path, actor_fallback="Polly")
    assert len(envelopes) == 1
    assert envelopes[0].actor == "Polly"
    assert envelopes[0].role == MessageRole.ASSISTANT
    assert envelopes[0].metadata["model"] == "claude-opus-4-7"


def test_skips_user_turn_with_empty_text(tmp_path: Path) -> None:
    # Spec: ingestor only emits user_turn when text is truthy, but we
    # still test the parser is tolerant of an empty-string payload.
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("user_turn", text="")])
    envelopes = parse_events_jsonl(events_path)
    # Parser still emits — empty text is allowed; the renderer decides.
    assert len(envelopes) == 1
    assert envelopes[0].text == ""


def test_token_usage_events_are_dropped(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event("user_turn", text="Hi"),
        _claude_event("token_usage", usage={"total_tokens": 42}),
    ])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1
    assert envelopes[0].type == MessageType.TEXT


def test_session_state_events_are_dropped(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _codex_event("session_state", state="started"),
        _codex_event("user_turn", text="Hello"),
    ])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1
    assert envelopes[0].type == MessageType.TEXT
    assert envelopes[0].text == "Hello"


# ---------------------------------------------------------------------------
# Extended-thinking (Anthropic ``thinking`` content blocks) — gated by
# ``include_thinking``. See GitHub #2048.
# ---------------------------------------------------------------------------


def test_thinking_envelope_emitted_when_flag_true(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "thinking",
        text="Let me think about this...",
        signature="opaque-sig-xyz",
        raw={
            "type": "thinking",
            "thinking": "Let me think about this...",
            "signature": "opaque-sig-xyz",
        },
    )])
    _parse_cache_clear()
    envelopes = parse_events_jsonl(
        events_path, actor_fallback="Polly", include_thinking=True,
    )
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.type == ParserInternalType.THINKING
    # ParserInternalType is deliberately separate from the public
    # MessageType catalog (#2082); the route filters it out before
    # serialization so the wire enum stays closed.
    assert env.type not in {member for member in MessageType}
    assert env.role == MessageRole.ASSISTANT
    assert env.actor == "Polly"
    assert env.text == "Let me think about this..."
    assert env.metadata["provider"] == "claude"
    assert env.metadata["model"] == "claude-opus-4-7"
    assert env.metadata["signature"] == "opaque-sig-xyz"


def test_thinking_envelope_dropped_when_flag_false(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event(
            "thinking",
            text="Hidden by default.",
            signature="opaque",
            raw={"type": "thinking", "thinking": "Hidden by default.", "signature": "opaque"},
        ),
        _claude_event("assistant_turn", text="Public reply."),
    ])
    _parse_cache_clear()
    envelopes = parse_events_jsonl(events_path, actor_fallback="Polly")
    # Default ``include_thinking=False`` drops the thinking envelope.
    assert [env.type for env in envelopes] == [MessageType.TEXT]
    assert envelopes[0].text == "Public reply."


def test_thinking_cache_does_not_leak_between_flag_values(tmp_path: Path) -> None:
    # The mtime cache must key on ``include_thinking`` so a False call
    # doesn't poison a subsequent True call (or vice versa).
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event(
            "thinking",
            text="Thought one.",
            signature="",
            raw={"type": "thinking", "thinking": "Thought one.", "signature": ""},
        ),
        _claude_event("assistant_turn", text="Spoken."),
    ])
    _parse_cache_clear()
    # First: default False, only assistant_turn surfaces.
    no_thinking = parse_events_jsonl(events_path)
    assert [env.type for env in no_thinking] == [MessageType.TEXT]
    # Same path, same mtime — flipping the flag must return a thinking
    # envelope, not the cached non-thinking list.
    with_thinking = parse_events_jsonl(events_path, include_thinking=True)
    assert [env.type for env in with_thinking] == [
        ParserInternalType.THINKING, MessageType.TEXT,
    ]


# ---------------------------------------------------------------------------
# Tool use / tool result pairing
# ---------------------------------------------------------------------------


def test_bash_tool_call_formats_with_command_summary(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_111",
        name="Bash",
        input={"command": "git status", "description": "Show working tree"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.type == MessageType.TOOL_USE
    assert env.text == "[Bash] git status"
    assert env.metadata["tool_use_id"] == "toolu_111"
    assert env.metadata["tool_name"] == "Bash"
    assert env.metadata["tool_input"]["command"] == "git status"


def test_bash_multiline_command_truncates_to_first_line(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_222",
        name="Bash",
        input={"command": "git add foo\ngit commit -m 'x'", "description": "two lines"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "[Bash] git add foo"


def test_read_tool_call_summarizes_file_path(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_333",
        name="Read",
        input={"file_path": "/etc/hosts"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "[Read] /etc/hosts"


def test_write_edit_glob_grep_tool_summary_includes_target() -> None:
    cases = [
        ("Write", {"file_path": "/tmp/x"}, "[Write] /tmp/x"),
        ("Edit", {"file_path": "/tmp/y"}, "[Edit] /tmp/y"),
        ("Glob", {"pattern": "**/*.py"}, "[Glob] **/*.py"),
        ("Grep", {"pattern": "TODO"}, "[Grep] TODO"),
    ]
    for tool_name, tool_input, expected in cases:
        assert _format_tool_use_summary(tool_name, tool_input) == expected


def test_webfetch_summary_includes_url() -> None:
    summary = _format_tool_use_summary("WebFetch", {"url": "https://x.example"})
    assert summary == "[WebFetch] https://x.example"


def test_websearch_summary_includes_query() -> None:
    summary = _format_tool_use_summary("WebSearch", {"query": "rust async"})
    assert summary == "[WebSearch] rust async"


def test_unknown_tool_summary_uses_description_or_name() -> None:
    assert _format_tool_use_summary("CustomTool", {"description": "fetch X"}) == "[CustomTool] fetch X"
    assert _format_tool_use_summary("CustomTool", {}) == "[CustomTool]"


def test_empty_tool_name_returns_generic_marker() -> None:
    assert _format_tool_use_summary("", {}) == "[tool]"


def test_tool_result_links_back_via_tool_use_id(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event(
            "tool_call",
            type="tool_use",
            id="toolu_999",
            name="Bash",
            input={"command": "ls"},
        ),
        _claude_event(
            "tool_result",
            type="tool_result",
            tool_use_id="toolu_999",
            content=[{"type": "text", "text": "foo\nbar"}],
            is_error=False,
        ),
    ])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 2
    use_env, result_env = envelopes
    assert use_env.metadata["tool_use_id"] == "toolu_999"
    assert result_env.metadata["tool_use_id"] == "toolu_999"
    assert result_env.text == "foo\nbar"
    assert result_env.metadata["is_error"] is False
    assert result_env.role == MessageRole.TOOL


def test_tool_result_preserves_raw_content_blocks(tmp_path: Path) -> None:
    blocks = [
        {"type": "text", "text": "summary"},
        {"type": "image", "source": {"data": "..."}},
    ]
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_result",
        type="tool_result",
        tool_use_id="t",
        content=blocks,
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].metadata["content"] == blocks


def test_tool_result_marks_errors(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_result",
        type="tool_result",
        tool_use_id="toolu_e",
        content=[{"type": "text", "text": "exit 1"}],
        is_error=True,
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].metadata["is_error"] is True


# ---------------------------------------------------------------------------
# Subagent spawn / result pairing
# ---------------------------------------------------------------------------


def test_task_tool_call_emits_subagent_spawn_envelope(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_sub1",
        name="Task",
        input={
            "description": "Fix #1234 sweep dedupe",
            "prompt": "Read the issue and fix the dedupe...",
            "subagent_type": "general-purpose",
            "isolation": "worktree",
            "run_in_background": True,
        },
    )])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.type == MessageType.SUBAGENT_SPAWN
    assert env.text == "[subagent] Fix #1234 sweep dedupe"
    assert env.metadata["subagent_id"] == "toolu_sub1"
    assert env.metadata["subagent_type"] == "general-purpose"
    assert env.metadata["isolation"] == "worktree"
    assert env.metadata["run_in_background"] is True
    assert "Read the issue" in env.metadata["prompt"]


def test_agent_tool_name_also_treated_as_subagent(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_sub_alt",
        name="Agent",
        input={"description": "alt"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].type == MessageType.SUBAGENT_SPAWN


def test_task_tool_result_with_task_notification_emits_subagent_result(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event(
            "tool_call",
            type="tool_use",
            id="toolu_sub2",
            name="Task",
            input={"description": "Run something"},
        ),
        _claude_event(
            "tool_result",
            type="tool_result",
            tool_use_id="toolu_sub2",
            content=[
                {"type": "text", "text": "PR #1235 pushed."},
                {
                    "type": "task-notification",
                    "task-id": "task-99",
                    "output-file": "/tmp/transcripts/task-99/events.jsonl",
                    "duration-ms": 12345,
                    "total-tokens": 4500,
                    "worktree-path": "/Users/x/wt",
                },
            ],
        ),
    ])
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 2
    spawn_env, result_env = envelopes
    assert spawn_env.type == MessageType.SUBAGENT_SPAWN
    assert result_env.type == MessageType.SUBAGENT_RESULT
    # Subagent linking: same id on both envelopes.
    assert spawn_env.metadata["subagent_id"] == result_env.metadata["subagent_id"]
    assert result_env.metadata["task_id"] == "task-99"
    assert result_env.metadata["output_file"].endswith("events.jsonl")
    assert result_env.metadata["duration_ms"] == 12345
    assert result_env.metadata["total_tokens"] == 4500
    assert result_env.metadata["worktree_path"] == "/Users/x/wt"


def test_looks_like_subagent_result_detects_task_notification_block() -> None:
    assert _looks_like_subagent_result([
        {"type": "task-notification", "task-id": "t-1", "output-file": "/x"},
    ]) is True
    assert _looks_like_subagent_result([
        {"task-id": "t-1", "output-file": "/x"},  # missing explicit type
    ]) is True
    assert _looks_like_subagent_result([{"type": "text", "text": "x"}]) is False
    assert _looks_like_subagent_result("not a list") is False


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------


def test_ask_user_question_emits_ask_user_envelope(tmp_path: Path) -> None:
    questions = [{
        "question": "Which library should we use?",
        "header": "Library",
        "multiSelect": False,
        "options": [
            {"label": "date-fns", "description": "Modular"},
            {"label": "dayjs", "description": "Lightweight"},
        ],
    }]
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_ask",
        name="AskUserQuestion",
        input={"questions": questions},
    )])
    envelopes = parse_events_jsonl(events_path)
    env = envelopes[0]
    assert env.type == MessageType.ASK_USER
    assert env.text == "Which library should we use?"
    assert env.metadata["tool_use_id"] == "toolu_ask"
    assert env.metadata["answered"] is False
    assert env.metadata["answers"] is None
    assert env.metadata["questions"][0]["options"][0]["label"] == "date-fns"


def test_ask_user_with_missing_questions_uses_fallback_text(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_ask_empty",
        name="AskUserQuestion",
        input={"questions": "not a list"},  # malformed
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "[ask_user]"
    assert envelopes[0].metadata["questions"] == []


# ---------------------------------------------------------------------------
# SendUserFile
# ---------------------------------------------------------------------------


def test_send_user_file_with_multiple_files(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_file",
        name="SendUserFile",
        input={
            "files": ["~/Desktop/a.pdf", "~/Desktop/b.pdf"],
            "caption": "Reports",
            "status": "proactive",
        },
    )])
    envelopes = parse_events_jsonl(events_path)
    env = envelopes[0]
    assert env.type == MessageType.FILE
    assert "a.pdf" in env.text and "b.pdf" in env.text
    assert env.metadata["files"] == ["~/Desktop/a.pdf", "~/Desktop/b.pdf"]
    assert env.metadata["caption"] == "Reports"
    assert env.metadata["status"] == "proactive"


def test_send_user_file_accepts_single_string(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_file2",
        name="SendUserFile",
        input={"files": "~/Desktop/only.pdf"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].metadata["files"] == ["~/Desktop/only.pdf"]


def test_send_user_file_with_no_files_renders_placeholder(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_file3",
        name="SendUserFile",
        input={"files": []},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "[file] (no files)"


# ---------------------------------------------------------------------------
# System events: error, turn_end
# ---------------------------------------------------------------------------


def test_error_event_becomes_system_event(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "error",
        error={"message": "boom", "kind": "fatal"},
    )])
    envelopes = parse_events_jsonl(events_path)
    env = envelopes[0]
    assert env.type == MessageType.SYSTEM_EVENT
    assert env.metadata["subtype"] == "error"
    assert env.text == "boom"


def test_error_event_handles_string_error(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("error", error="oops")])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "oops"


def test_turn_end_event_is_system_event(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_codex_event("turn_end", reason="done")])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].type == MessageType.SYSTEM_EVENT
    assert envelopes[0].metadata["subtype"] == "turn_end"


# ---------------------------------------------------------------------------
# Codex shapes
# ---------------------------------------------------------------------------


def test_codex_user_turn_parses_to_text(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_codex_event("user_turn", text="hi codex")])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].text == "hi codex"
    assert envelopes[0].metadata["provider"] == "codex"


def test_codex_assistant_turn_parses_to_text(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_codex_event(
        "assistant_turn", text="codex reply",
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].role == MessageRole.ASSISTANT
    assert envelopes[0].text == "codex reply"


def test_codex_tool_call_renders_with_generic_name(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_codex_event(
        "tool_call",
        type="local_shell",
        command="ls -la",
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].type == MessageType.TOOL_USE
    assert "[local_shell]" in envelopes[0].text


def test_codex_tool_result_extracts_output_text(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_codex_event(
        "tool_result",
        type="local_shell_result",
        output="file1\nfile2",
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].type == MessageType.TOOL_RESULT
    assert envelopes[0].text == "file1\nfile2"


# ---------------------------------------------------------------------------
# Helper: _extract_text_from_blocks
# ---------------------------------------------------------------------------


def test_extract_text_from_string() -> None:
    assert _extract_text_from_blocks("hello") == "hello"


def test_extract_text_from_list_of_blocks() -> None:
    blocks = [
        {"type": "text", "text": "part 1"},
        {"type": "text", "text": "part 2"},
    ]
    assert _extract_text_from_blocks(blocks) == "part 1\npart 2"


def test_extract_text_from_dict() -> None:
    assert _extract_text_from_blocks({"text": "wrapped"}) == "wrapped"


def test_extract_text_returns_empty_for_other_types() -> None:
    assert _extract_text_from_blocks(None) == ""
    assert _extract_text_from_blocks(42) == ""


def test_extract_text_skips_non_text_blocks() -> None:
    blocks = [
        {"type": "image", "source": "..."},
        {"type": "text", "text": "kept"},
    ]
    assert _extract_text_from_blocks(blocks) == "kept"


# ---------------------------------------------------------------------------
# Edge cases: malformed lines, missing file, empty file
# ---------------------------------------------------------------------------


def test_parse_skips_malformed_json_lines(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(_claude_event("user_turn", text="ok")) + "\n"
        + "not json at all\n"
        + json.dumps(_claude_event("assistant_turn", text="reply")) + "\n",
    )
    envelopes = parse_events_jsonl(events_path)
    assert [e.text for e in envelopes] == ["ok", "reply"]


def test_parse_skips_blank_lines(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n\n"
        + json.dumps(_claude_event("user_turn", text="hi")) + "\n"
        + "\n",
    )
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1


def test_parse_skips_non_object_lines(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "[1, 2, 3]\n"
        + json.dumps(_claude_event("user_turn", text="ok")) + "\n",
    )
    envelopes = parse_events_jsonl(events_path)
    assert len(envelopes) == 1


def test_parse_missing_file_returns_empty_list(tmp_path: Path) -> None:
    envelopes = parse_events_jsonl(tmp_path / "nope.jsonl")
    assert envelopes == []


def test_parse_empty_file_returns_empty_list(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    envelopes = parse_events_jsonl(events_path)
    assert envelopes == []


# ---------------------------------------------------------------------------
# mtime cache (issue #2069)
#
# The history endpoint polls every ~5s and the parse body forward-
# readlines the full archive each call. For unchanged files the
# memoized envelopes must be returned without re-running the heavy
# event-to-envelope translation. ``strict=True`` callers (validation
# paths) MUST bypass the cache so they still see fresh parse errors.
# ---------------------------------------------------------------------------


def test_parse_unchanged_file_hits_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event("user_turn", text="hi"),
        _claude_event("assistant_turn", text="hello"),
    ])

    parse_calls = {"count": 0}
    real_event_to_envelopes = transcripts_module._event_to_envelopes

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        parse_calls["count"] += 1
        return real_event_to_envelopes(*args, **kwargs)

    monkeypatch.setattr(transcripts_module, "_event_to_envelopes", counting)

    first = parse_events_jsonl(events_path)
    second = parse_events_jsonl(events_path)
    third = parse_events_jsonl(events_path)

    # Two events in the archive -> 2 _event_to_envelopes calls on the
    # initial parse, zero on subsequent calls when mtime is unchanged.
    assert parse_calls["count"] == 2
    assert [env.text for env in first] == ["hi", "hello"]
    assert [env.text for env in second] == ["hi", "hello"]
    assert [env.text for env in third] == ["hi", "hello"]
    # Each call returns a fresh list — callers sort in place
    # (_apply_filters_and_paginate), so handing them the cached list
    # directly would corrupt the cache.
    assert first is not second
    assert second is not third


def test_parse_cache_invalidates_on_mtime_change(
    tmp_path: Path,
) -> None:
    _parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("user_turn", text="one")])
    first = parse_events_jsonl(events_path)
    assert [env.text for env in first] == ["one"]

    # Append a new event AND bump mtime so the cache invalidates.
    with events_path.open("a") as handle:
        handle.write(json.dumps(_claude_event("user_turn", text="two")) + "\n")
    new_mtime = events_path.stat().st_mtime + 5.0
    import os
    os.utime(events_path, (new_mtime, new_mtime))

    second = parse_events_jsonl(events_path)
    assert [env.text for env in second] == ["one", "two"]


def test_parse_cache_skips_strict_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """``strict=True`` callers want fresh parses (validation surface)."""
    _parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("user_turn", text="a")])

    parse_calls = {"count": 0}
    real_event_to_envelopes = transcripts_module._event_to_envelopes

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        parse_calls["count"] += 1
        return real_event_to_envelopes(*args, **kwargs)

    monkeypatch.setattr(transcripts_module, "_event_to_envelopes", counting)

    parse_events_jsonl(events_path, strict=True)
    parse_events_jsonl(events_path, strict=True)
    parse_events_jsonl(events_path, strict=True)
    # One event * 3 strict calls = 3 conversions (no caching).
    assert parse_calls["count"] == 3


def test_parse_cache_distinguishes_actor_fallback(
    tmp_path: Path,
) -> None:
    """Different actor_fallback must re-parse so the persona is baked in."""
    _parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event("assistant_turn", text="hi")])

    polly_run = parse_events_jsonl(events_path, actor_fallback="Polly")
    worker_run = parse_events_jsonl(events_path, actor_fallback="worker")

    assert polly_run[0].actor == "Polly"
    assert worker_run[0].actor == "worker"


def test_parse_cache_lru_evicts_oldest_when_full(tmp_path: Path) -> None:
    """Cap at _PARSE_CACHE_MAX entries; oldest insertion evicts first."""
    _parse_cache_clear()
    # Fill the cache to cap by parsing one file per entry.
    for idx in range(transcripts_module._PARSE_CACHE_MAX):
        path = tmp_path / f"events-{idx}.jsonl"
        _write_events(path, [_claude_event("user_turn", text=f"e{idx}")])
        parse_events_jsonl(path)
    assert len(transcripts_module._PARSE_CACHE) == transcripts_module._PARSE_CACHE_MAX
    # Cache key is ``(events_path, include_thinking)`` after #2048.
    oldest_key = (tmp_path / "events-0.jsonl", False)
    assert oldest_key in transcripts_module._PARSE_CACHE

    # One more parse pushes us past the cap -> oldest evicts.
    overflow = tmp_path / "events-overflow.jsonl"
    _write_events(overflow, [_claude_event("user_turn", text="overflow")])
    parse_events_jsonl(overflow)
    assert len(transcripts_module._PARSE_CACHE) == transcripts_module._PARSE_CACHE_MAX
    assert oldest_key not in transcripts_module._PARSE_CACHE
    assert (overflow, False) in transcripts_module._PARSE_CACHE


# ---------------------------------------------------------------------------
# Tail-read fast path (issue #2070)
#
# The cockpit polls ``?limit=50&direction=desc`` every few seconds.
# Before tail-read, the parser forward-readlined the full archive
# every call — for 2,800-line operator transcripts the cost dominated
# poll latency. ``parse_events_jsonl_tail`` reads from EOF in chunks
# so cost scales with the requested limit, not the file size.
# ---------------------------------------------------------------------------


def _write_user_turn_lines(path: Path, count: int) -> None:
    """Write ``count`` single-line user_turn events to ``path``.

    Each event payload is unique so the test can verify ordering
    (event index encoded in the text).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for idx in range(count):
            handle.write(
                json.dumps(_claude_event("user_turn", text=f"line-{idx}"))
                + "\n",
            )


def test_parse_tail_returns_last_n_envelopes(tmp_path: Path) -> None:
    """Tail of a 1k-line archive matches ``parse_events_jsonl(...)[-N:]``."""
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 1000)

    tail = parse_events_jsonl_tail(events_path, limit=50)
    full_tail = parse_events_jsonl(events_path)[-50:]

    # Clear the cache so the next assertion isn't comparing a tail
    # against a cached full parse from above (the tail short-circuits
    # to cache when populated).
    assert len(tail) == 50
    assert [env.text for env in tail] == [env.text for env in full_tail]
    # Last envelope is the newest line we wrote.
    assert tail[-1].text == "line-999"
    assert tail[0].text == "line-950"


def test_parse_tail_handles_file_smaller_than_chunk(tmp_path: Path) -> None:
    """Whole-file fits in the first chunk: still returns the last N."""
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 10)

    tail = parse_events_jsonl_tail(events_path, limit=5)
    assert [env.text for env in tail] == [f"line-{i}" for i in range(5, 10)]


def test_parse_tail_limit_exceeds_file_returns_all(tmp_path: Path) -> None:
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 3)

    tail = parse_events_jsonl_tail(events_path, limit=50)
    assert [env.text for env in tail] == ["line-0", "line-1", "line-2"]


def test_parse_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    transcripts_module._parse_cache_clear()
    assert parse_events_jsonl_tail(tmp_path / "missing.jsonl", limit=50) == []


def test_parse_tail_empty_file_returns_empty(tmp_path: Path) -> None:
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    assert parse_events_jsonl_tail(events_path, limit=50) == []


def test_parse_tail_zero_limit_returns_empty(tmp_path: Path) -> None:
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 5)
    assert parse_events_jsonl_tail(events_path, limit=0) == []


def test_parse_tail_uses_mtime_cache_when_populated(tmp_path: Path) -> None:
    """A populated mtime cache short-circuits the tail-read."""
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 200)

    # Populate the cache by calling the full parser.
    parse_events_jsonl(events_path)
    # Cache key is ``(events_path, include_thinking)`` after #2048 +
    # #2070 composition.
    assert (events_path, False) in transcripts_module._PARSE_CACHE

    # Tail should now read from the cached list, not the disk.
    tail = parse_events_jsonl_tail(events_path, limit=10)
    assert len(tail) == 10
    assert tail[-1].text == "line-199"


def test_parse_tail_does_not_populate_mtime_cache(tmp_path: Path) -> None:
    """Tail returns a partial slice — caching it would poison the cache."""
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 100)

    parse_events_jsonl_tail(events_path, limit=10)
    assert (events_path, False) not in transcripts_module._PARSE_CACHE


def test_parse_tail_falls_back_for_pretty_printed_event(tmp_path: Path) -> None:
    """Pretty-printed JSON (multi-line event) trips the defensive fallback.

    The tail parser is line-oriented (one JSON object per line). A
    pretty-printed event spans multiple lines, so ``json.loads`` on
    each line raises ``JSONDecodeError`` — the tail parser then sees
    fewer than ``limit`` envelopes and either widens or falls back to
    the forward parser. The forward parser also sees those lines as
    malformed (the ingestor never emits pretty-printed events), but
    the surrounding single-line events are still recovered. The
    contract under test is "no exception escapes" — the route never
    sees a parser crash for an oddly-shaped archive.
    """
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    # Mix single-line + pretty-printed events. The pretty-printed
    # block exercises the chunk-boundary code path: it spans multiple
    # lines, none of which parse as standalone JSON.
    pretty_event = json.dumps(
        _claude_event("user_turn", text="pretty"),
        indent=2,
    )
    single_lines = [
        json.dumps(_claude_event("user_turn", text=f"good-{idx}"))
        for idx in range(20)
    ]
    body = "\n".join(single_lines[:10]) + "\n" + pretty_event + "\n" + (
        "\n".join(single_lines[10:]) + "\n"
    )
    events_path.write_text(body)

    # Must not raise.
    tail = parse_events_jsonl_tail(events_path, limit=5)
    # The 5 most-recent valid envelopes are the last 5 good lines.
    assert [env.text for env in tail] == [
        f"good-{idx}" for idx in range(15, 20)
    ]


def test_parse_tail_falls_back_for_single_huge_line(tmp_path: Path) -> None:
    """A single line larger than 1MB defeats the chunk progression.

    The tail parser bails to the forward parser when even the largest
    chunk lands inside one line (no newline in the chunk window).
    """
    transcripts_module._parse_cache_clear()
    events_path = tmp_path / "events.jsonl"
    # 1.5MB of single-line text (no newlines) — larger than the
    # widest chunk in ``_TAIL_CHUNK_PROGRESSION``.
    huge = "x" * (1_500_000)
    events_path.write_text(huge + "\n")
    # No valid envelopes, but must not raise.
    tail = parse_events_jsonl_tail(events_path, limit=10)
    assert tail == []


def test_parse_tail_perf_beats_full_parse_on_10k_archive(tmp_path: Path) -> None:
    """Tail must be ≥50× faster than the full forward parse on a 10k file.

    The mtime cache is cleared between calls so we're comparing
    cold-path cost (the exact case the optimization targets).
    """
    events_path = tmp_path / "events.jsonl"
    _write_user_turn_lines(events_path, 10_000)

    # Warm Python's I/O caches by reading the file once before
    # measuring. The OS page cache makes the second read deterministic
    # — we're measuring CPU + parse cost, not first-touch I/O.
    transcripts_module._parse_cache_clear()
    parse_events_jsonl(events_path)

    # Cold-path full parse: cache cleared, must re-read + re-translate
    # the entire file.
    transcripts_module._parse_cache_clear()
    full_start = time.perf_counter()
    full_envelopes = parse_events_jsonl(events_path)
    full_elapsed = time.perf_counter() - full_start
    assert len(full_envelopes) == 10_000

    # Cold-path tail: same cleared cache, must return the same final
    # 50 envelopes as ``full_envelopes[-50:]``.
    transcripts_module._parse_cache_clear()
    tail_start = time.perf_counter()
    tail_envelopes = parse_events_jsonl_tail(events_path, limit=50)
    tail_elapsed = time.perf_counter() - tail_start
    assert len(tail_envelopes) == 50
    assert [env.text for env in tail_envelopes] == [
        env.text for env in full_envelopes[-50:]
    ]

    # Issue #2070 acceptance: tail wall-clock under 5ms.
    assert tail_elapsed < 0.005, (
        f"tail took {tail_elapsed * 1000:.2f}ms — expected <5ms"
    )
    # And at least 50× faster than the full parse. We compare ratios
    # rather than absolute times so the test stays robust on slower
    # CI machines (full parse scales with file size; tail does not).
    speedup = full_elapsed / max(tail_elapsed, 1e-9)
    assert speedup >= 50.0, (
        f"speedup was {speedup:.1f}× — expected ≥50× "
        f"(full {full_elapsed * 1000:.2f}ms vs tail "
        f"{tail_elapsed * 1000:.2f}ms)"
    )


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


def test_is_archive_stale_true_for_missing_file(tmp_path: Path) -> None:
    assert is_archive_stale(tmp_path / "missing.jsonl") is True
    assert is_archive_stale(None) is True


def test_is_archive_stale_true_for_old_file(tmp_path: Path) -> None:
    path = tmp_path / "old.jsonl"
    path.write_text("x")
    # Simulate an old file by injecting ``now`` 120s in the future.
    assert is_archive_stale(
        path, threshold_seconds=60, now=time.time() + 120,
    ) is True


def test_is_archive_stale_false_for_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "fresh.jsonl"
    path.write_text("x")
    assert is_archive_stale(path, threshold_seconds=60) is False


def test_stale_threshold_default_matches_spec() -> None:
    # Spec §4.7 says ">60s" — module constant should match so the
    # router can document the spec value without drift.
    assert STALE_THRESHOLD_SECONDS == 60.0


# ---------------------------------------------------------------------------
# resolve_transcript_path
# ---------------------------------------------------------------------------


def test_resolve_transcript_returns_none_when_no_cwd_match(
    tmp_path: Path,
) -> None:
    # Codex review #2044 — explicit unresolved instead of falling back
    # to "freshest events.jsonl under the project root". Without a cwd
    # match we MUST return None so the API never cross-attaches a
    # different surface's transcript.
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    older = transcripts / "session-old"
    newer = transcripts / "session-new"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    old_event = _claude_event("user_turn", text="x")
    old_event["cwd"] = str(tmp_path / "elsewhere-a")
    (older / "events.jsonl").write_text(json.dumps(old_event) + "\n")
    time.sleep(0.05)  # Filesystem mtime resolution.
    new_event = _claude_event("user_turn", text="y")
    new_event["cwd"] = str(tmp_path / "elsewhere-b")
    (newer / "events.jsonl").write_text(json.dumps(new_event) + "\n")
    # No cwd supplied → cannot fingerprint → None.
    assert resolve_transcript_path(project_root) is None
    # cwd supplied but no match → still None (no freshest fallback).
    assert resolve_transcript_path(
        project_root, cwd=str(tmp_path / "nowhere"),
    ) is None


def test_resolve_transcript_prefers_cwd_match(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    matching = transcripts / "session-match"
    unrelated = transcripts / "session-unrelated"
    matching.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    cwd_match = str(tmp_path / "match-cwd")
    cwd_other = str(tmp_path / "other-cwd")
    Path(cwd_match).mkdir()
    Path(cwd_other).mkdir()
    # Put the matching dir with the OLDER mtime to confirm cwd wins.
    matching_event = _claude_event("user_turn", text="hi")
    matching_event["cwd"] = cwd_match
    (matching / "events.jsonl").write_text(json.dumps(matching_event) + "\n")
    time.sleep(0.05)
    other_event = _claude_event("user_turn", text="hi")
    other_event["cwd"] = cwd_other
    (unrelated / "events.jsonl").write_text(json.dumps(other_event) + "\n")
    resolved = resolve_transcript_path(project_root, cwd=cwd_match)
    assert resolved is not None
    assert resolved.parent.name == "session-match"


def test_resolve_transcript_returns_none_when_root_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "no-transcripts"
    project_root.mkdir()
    assert resolve_transcript_path(project_root) is None


def test_resolve_transcript_returns_none_when_no_event_files(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    (transcripts / "empty-session").mkdir(parents=True)
    assert resolve_transcript_path(project_root) is None


def test_resolve_transcript_skips_tasks_subdir(tmp_path: Path) -> None:
    # ``tasks/`` holds raw provider JSONLs archived by SessionManager;
    # it's never the normalized ``events.jsonl`` shape.
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    tasks_dir = transcripts / "tasks"
    tasks_dir.mkdir(parents=True)
    tasks_event = _claude_event("user_turn", text="x")
    tasks_event["cwd"] = str(tmp_path / "anywhere")
    (tasks_dir / "events.jsonl").write_text(json.dumps(tasks_event) + "\n")
    # Even with a matching cwd, the tasks/ subdir must be ignored.
    assert resolve_transcript_path(
        project_root, cwd=str(tmp_path / "anywhere"),
    ) is None


def test_resolve_transcript_filters_by_account_name(tmp_path: Path) -> None:
    # Two surfaces sharing the same cwd MUST be disambiguated by their
    # account_name fingerprint. Without this filter the older sibling
    # would cross-attach to the newer surface's transcript.
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    shared_cwd = str(tmp_path / "shared")
    Path(shared_cwd).mkdir()
    main_event = _claude_event("user_turn", text="ops")
    main_event["account_name"] = "claude_main"
    main_event["cwd"] = shared_cwd
    (transcripts / "session-main").mkdir(parents=True)
    (transcripts / "session-main" / "events.jsonl").write_text(
        json.dumps(main_event) + "\n",
    )
    time.sleep(0.05)
    alt_event = _claude_event("user_turn", text="arch")
    alt_event["account_name"] = "claude_alt"
    alt_event["cwd"] = shared_cwd
    (transcripts / "session-alt").mkdir(parents=True)
    (transcripts / "session-alt" / "events.jsonl").write_text(
        json.dumps(alt_event) + "\n",
    )
    main_resolved = resolve_transcript_path(
        project_root, cwd=shared_cwd, account_name="claude_main",
    )
    assert main_resolved is not None
    assert main_resolved.parent.name == "session-main"
    alt_resolved = resolve_transcript_path(
        project_root, cwd=shared_cwd, account_name="claude_alt",
    )
    assert alt_resolved is not None
    assert alt_resolved.parent.name == "session-alt"


def test_build_session_index_skips_unreadable_events(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    transcripts.mkdir(parents=True)
    # Empty events.jsonl — no fingerprint, must be skipped.
    (transcripts / "session-empty").mkdir()
    (transcripts / "session-empty" / "events.jsonl").write_text("")
    # Malformed first line — must be skipped.
    (transcripts / "session-bad").mkdir()
    (transcripts / "session-bad" / "events.jsonl").write_text("garbage\n")
    # Well-formed line — kept.
    (transcripts / "session-ok").mkdir()
    good = _claude_event("user_turn", text="ok")
    good["cwd"] = str(tmp_path / "cwd-ok")
    (transcripts / "session-ok" / "events.jsonl").write_text(
        json.dumps(good) + "\n",
    )
    index = build_session_index(transcripts)
    assert {entry.session_id for entry in index} == {"session-ok"}


def test_lookup_returns_none_without_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    transcripts = project_root / ".pollypm" / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "session-a").mkdir()
    event = _claude_event("user_turn", text="hi")
    event["cwd"] = str(tmp_path / "x")
    (transcripts / "session-a" / "events.jsonl").write_text(
        json.dumps(event) + "\n",
    )
    index = build_session_index(transcripts)
    assert lookup_transcript_path(index, cwd=None) is None
    assert lookup_transcript_path(index, cwd="") is None


# ---------------------------------------------------------------------------
# Envelope id stability
# ---------------------------------------------------------------------------


def test_envelope_ids_are_unique_within_file(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        _claude_event("user_turn", text="one"),
        _claude_event("user_turn", text="two"),
        _claude_event("user_turn", text="three"),
    ])
    envelopes = parse_events_jsonl(events_path)
    ids = [e.id for e in envelopes]
    assert len(set(ids)) == len(ids)


def test_tool_call_envelope_id_uses_tool_use_id(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_claude_event(
        "tool_call",
        type="tool_use",
        id="toolu_stable_123",
        name="Bash",
        input={"command": "echo"},
    )])
    envelopes = parse_events_jsonl(events_path)
    assert envelopes[0].id == "msg_toolu_stable_123"
