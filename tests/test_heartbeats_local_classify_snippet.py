"""Regression coverage for issue #1039.

The heartbeat ``_classify`` snippet rendering used to leak ANSI control
fragments, transcript dividers, and bare bullet leaders into the
user-facing ``pm alerts`` reason text. ``_select_snippet`` is the
extracted helper that walks the transcript tail and skips uninformative
lines. This test fixture pins each of the 8 live alert samples I
collected over an overnight loop so the regression cannot slip back in.
"""

from __future__ import annotations

import re

import pytest

from pollypm.heartbeats.local import _select_snippet


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _prep(raw: str) -> str:
    """Mirror the ANSI-strip the production ``_classify`` runs upstream."""

    return _ANSI_RE.sub("", raw)


# Each fixture is a transcript-tail sample and the asserted clean snippet
# substring. Several samples include an ANSI escape that previously got
# truncated to ``[2`` / ``[4`` by the 120-char slice; the fix strips ANSI
# *before* picking the snippet so the codes vanish entirely.
LIVE_ALERT_FIXTURES: list[tuple[str, str]] = [
    # Pure divider tail — should fall back to an earlier informative line
    # rather than rendering bare ``─`` characters.
    (
        "Working on follow-up.\n"
        "─" * 105,
        "Working on follow-up.",
    ),
    (
        "Working on follow-up.\n"
        "─" * 80,
        "Working on follow-up.",
    ),
    # Transcript ends with a blank line — must not render as empty snippet.
    (
        "Wrapping up the previous step.\n\n",
        "Wrapping up the previous step.",
    ),
    # Box-drawing leader line — leader is stripped, content kept.
    (
        "Checking the queue.\n"
        "  └ No tasks available.\n",
        "No tasks available.",
    ),
    # Bullet leader — leader is stripped, content kept.
    (
        "Re-running the queue probe.\n"
        "• Running pm task next -p booktalk again now.\n",
        "Running pm task next -p booktalk again now.",
    ),
    # ANSI fragment at end-of-line that would previously truncate to "[2".
    (
        "Probing the queue.\n"
        "• pm task next still returns: No tasks available.\x1b[2m\n",
        "pm task next still returns: No tasks available.",
    ),
    # ANSI fragment that would previously truncate to "[4".
    (
        "Re-checking the worker queue.\n"
        "• Checking the local worker queue again with the safe wrapper.\x1b[4m\n",
        "Checking the local worker queue again with the safe wrapper.",
    ),
    # Repeat of the booktalk box-drawing case from a separate worker
    # (camptown) — pinned so both renderings stay clean.
    (
        "Picked up the next ticket.\n"
        "  └ No tasks available.\n",
        "No tasks available.",
    ),
]


@pytest.mark.parametrize("raw, expected", LIVE_ALERT_FIXTURES)
def test_select_snippet_renders_clean_snippet(raw: str, expected: str) -> None:
    snippet = _select_snippet(_prep(raw))

    # No ANSI control sequences — full or truncated — survive.
    assert "\x1b" not in snippet
    assert "[2" not in snippet
    assert "[4" not in snippet

    # No pure divider runs. A short hyphen inside content (e.g. "follow-up")
    # is fine; a run of 4+ box-drawing chars is not.
    assert not re.search(r"[─━═]{2,}", snippet)
    assert not re.fullmatch(r"[─━═\-_=\s]+", snippet)

    # Leading bullets / box-drawing leaders are stripped.
    assert not snippet.startswith("•")
    assert not snippet.startswith("└")
    assert not snippet.startswith("├")

    # Snippet has the asserted informative payload.
    assert expected in snippet


def test_select_snippet_falls_back_when_no_informative_line() -> None:
    # All-divider transcript — no informative line anywhere.
    raw = ("─" * 80) + "\n" + ("─" * 60) + "\n   \n"
    snippet = _select_snippet(_prep(raw))
    assert snippet == "(continuing without a clear summary line)"


def test_select_snippet_empty_input_uses_fallback() -> None:
    assert _select_snippet("") == "(continuing without a clear summary line)"
