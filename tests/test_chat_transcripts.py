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
    STALE_THRESHOLD_SECONDS,
    is_archive_stale,
    parse_events_jsonl,
    resolve_transcript_path,
)
from pollypm.web_api.chat.transcripts import (
    _extract_text_from_blocks,
    _format_tool_use_summary,
    _looks_like_subagent_result,
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
