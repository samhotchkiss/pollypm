"""Unit tests for the chat-API tmux capture fallback (P1 spec §4.7).

Tests :func:`capture_envelopes` with a stub ``TmuxClient`` to assert:

- Each captured line becomes one envelope with ``type=text``
- ``metadata.from_capture`` is True
- ``metadata.session_name`` propagates
- Envelope ids are synthesized as ``cap_<hex>``
- Same line at same offset yields same id (stability)
- ANSI escape codes are stripped
- Leading/trailing blank lines are dropped
- Empty captures return an empty list
- Captures with no ``capture_pane`` method fail safe
- Captures that throw exceptions fail safe
"""

from __future__ import annotations

from typing import Any

from pollypm.web_api.chat import (
    MessageRole,
    MessageType,
    capture_envelopes,
    synthesize_capture_id,
)
from pollypm.web_api.chat.tmux_capture import (
    DEFAULT_CAPTURE_LINES,
    _strip_control_codes,
)


class _StubTmuxClient:
    """Records capture_pane calls and returns canned output."""

    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def capture_pane(self, target: str, lines: int = 200) -> str:
        self.calls.append({"target": target, "lines": lines})
        return self.output


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_capture_envelopes_one_per_line() -> None:
    tmux = _StubTmuxClient(output="line one\nline two\nline three\n")
    envelopes = capture_envelopes(
        tmux,
        session_name="operator",
        target="storage-closet:pm-operator",
    )
    assert len(envelopes) == 3
    texts = [e.text for e in envelopes]
    assert texts == ["line one", "line two", "line three"]


def test_capture_envelope_marked_text_type_and_from_capture() -> None:
    tmux = _StubTmuxClient(output="hello world\n")
    envelopes = capture_envelopes(
        tmux, session_name="operator", target="x",
    )
    env = envelopes[0]
    assert env.type == MessageType.TEXT
    assert env.role == MessageRole.ASSISTANT
    assert env.metadata["from_capture"] is True
    assert env.metadata["session_name"] == "operator"
    assert env.metadata["line_index"] == 0


def test_capture_envelope_id_uses_cap_prefix() -> None:
    tmux = _StubTmuxClient(output="x\n")
    env = capture_envelopes(tmux, session_name="s", target="t")[0]
    assert env.id.startswith("cap_")


def test_capture_envelope_actor_uses_fallback() -> None:
    tmux = _StubTmuxClient(output="hi\n")
    env = capture_envelopes(
        tmux, session_name="s", target="t", actor_fallback="Polly",
    )[0]
    assert env.actor == "Polly"


def test_capture_envelope_role_is_configurable() -> None:
    tmux = _StubTmuxClient(output="hi\n")
    env = capture_envelopes(
        tmux, session_name="s", target="t", role=MessageRole.USER,
    )[0]
    assert env.role == MessageRole.USER


def test_capture_envelope_timestamp_is_iso_z() -> None:
    tmux = _StubTmuxClient(output="hi\n")
    env = capture_envelopes(tmux, session_name="s", target="t")[0]
    assert env.ts.endswith("Z")
    # Year 2026 sanity bound — the real wall-clock should be after this.
    assert env.ts.startswith("20")


def test_capture_envelope_timestamp_override() -> None:
    tmux = _StubTmuxClient(output="hi\n")
    env = capture_envelopes(
        tmux, session_name="s", target="t",
        timestamp="2020-01-01T00:00:00Z",
    )[0]
    assert env.ts == "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Stability / id determinism
# ---------------------------------------------------------------------------


def test_synthesize_capture_id_is_stable() -> None:
    id1 = synthesize_capture_id("operator", 0, "hello")
    id2 = synthesize_capture_id("operator", 0, "hello")
    assert id1 == id2


def test_synthesize_capture_id_differs_for_different_content() -> None:
    id1 = synthesize_capture_id("operator", 0, "hello")
    id2 = synthesize_capture_id("operator", 0, "goodbye")
    assert id1 != id2


def test_synthesize_capture_id_differs_for_different_offset() -> None:
    id1 = synthesize_capture_id("operator", 0, "hello")
    id2 = synthesize_capture_id("operator", 1, "hello")
    assert id1 != id2


def test_synthesize_capture_id_differs_for_different_session() -> None:
    id1 = synthesize_capture_id("operator", 0, "hello")
    id2 = synthesize_capture_id("architect", 0, "hello")
    assert id1 != id2


# ---------------------------------------------------------------------------
# ANSI stripping / cleanup
# ---------------------------------------------------------------------------


def test_capture_strips_ansi_color_codes() -> None:
    output = "\x1b[31mred text\x1b[0m\n"
    tmux = _StubTmuxClient(output=output)
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    assert envelopes[0].text == "red text"


def test_capture_strips_c0_control_bytes_but_keeps_newlines_tabs() -> None:
    output = "hello\x00\x07\tworld\n"
    tmux = _StubTmuxClient(output=output)
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    assert "\x00" not in envelopes[0].text
    assert "\x07" not in envelopes[0].text
    assert "\t" in envelopes[0].text


def test_strip_control_codes_handles_complex_ansi() -> None:
    out = _strip_control_codes("\x1b[1;31mfoo\x1b[0m\x1b[2J\x1b[H")
    assert out == "foo"


def test_capture_drops_leading_blank_lines() -> None:
    tmux = _StubTmuxClient(output="\n\n\nfirst content\nsecond\n")
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    assert envelopes[0].text == "first content"
    assert envelopes[1].text == "second"


def test_capture_drops_trailing_blank_lines() -> None:
    tmux = _StubTmuxClient(output="first\nsecond\n\n\n\n")
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    assert envelopes[-1].text == "second"


def test_capture_preserves_internal_blank_lines() -> None:
    tmux = _StubTmuxClient(output="first\n\nthird\n")
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    texts = [e.text for e in envelopes]
    assert texts == ["first", "", "third"]


# ---------------------------------------------------------------------------
# Empty / failure cases
# ---------------------------------------------------------------------------


def test_capture_empty_output_returns_empty_list() -> None:
    tmux = _StubTmuxClient(output="")
    assert capture_envelopes(tmux, session_name="s", target="t") == []


def test_capture_with_none_client_returns_empty_list() -> None:
    assert capture_envelopes(None, session_name="s", target="t") == []


def test_capture_with_client_missing_capture_pane_returns_empty_list() -> None:
    class NoCapture:
        pass

    assert capture_envelopes(NoCapture(), session_name="s", target="t") == []


def test_capture_with_client_raising_returns_empty_list() -> None:
    class Boom:
        def capture_pane(self, target: str, lines: int = 200) -> str:
            raise RuntimeError("tmux dead")

    assert capture_envelopes(Boom(), session_name="s", target="t") == []


def test_capture_with_non_string_return_returns_empty_list() -> None:
    class WeirdReturn:
        def capture_pane(self, target: str, lines: int = 200) -> Any:
            return None

    assert capture_envelopes(WeirdReturn(), session_name="s", target="t") == []


# ---------------------------------------------------------------------------
# Capture depth argument
# ---------------------------------------------------------------------------


def test_capture_default_lines_matches_spec() -> None:
    # Spec §4.7: ``tmux capture-pane -p -S -3000``.
    assert DEFAULT_CAPTURE_LINES == 3000


def test_capture_passes_lines_arg_through_to_client() -> None:
    tmux = _StubTmuxClient(output="x\n")
    capture_envelopes(tmux, session_name="s", target="t", lines=500)
    assert tmux.calls == [{"target": "t", "lines": 500}]


def test_capture_passes_target_through_to_client() -> None:
    tmux = _StubTmuxClient(output="x\n")
    capture_envelopes(
        tmux, session_name="s", target="storage-closet:pm-operator",
    )
    assert tmux.calls[0]["target"] == "storage-closet:pm-operator"


# ---------------------------------------------------------------------------
# line_index monotonicity
# ---------------------------------------------------------------------------


def test_line_index_is_monotonic_per_envelope() -> None:
    tmux = _StubTmuxClient(output="a\nb\nc\n")
    envelopes = capture_envelopes(tmux, session_name="s", target="t")
    indices = [e.metadata["line_index"] for e in envelopes]
    assert indices == sorted(indices)
    # Strict monotonic (no dupes).
    assert len(set(indices)) == len(indices)
