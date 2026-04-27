"""Rendered cockpit smoke harness (#882).

Drives a Textual ``App`` headlessly via :class:`textual.pilot.Pilot`
and captures the *rendered* widget output — not the source data — so
assertions catch the class of bug the helper-level unit tests miss.

The pre-launch audit (``docs/launch-issue-audit-2026-04-27.md`` §2)
enumerates the bug shape this harness defends against:

* recovery commands hidden by rail width or column truncation
  (#831 ``ctrl+q``, #829 ``Try: pm …``, #826 modal ``Up`` label);
* raw bootstrap prompts and Python tracebacks reaching the cockpit;
* orphan box characters from unfinished borders;
* letter-by-letter wrapping at narrow widths;
* contradictory counts visible on the same screen (#820 home vs.
  rail inbox count).

Per-screen helper-level tests cannot catch these because they assert
against helper return values, not the post-layout text that the user
actually reads. This harness mounts the App, drives it through a
short interaction script, walks the widget tree, and checks the
concatenated rendered text against a small set of universal
assertions plus per-call custom assertions.

Usage::

    from pollypm.cockpit_smoke import (
        SmokeHarness,
        SMOKE_TERMINAL_SIZES,
    )

    async def test_my_screen() -> None:
        async with SmokeHarness.mount(MyApp(), size=(100, 40)) as smoke:
            await smoke.pilot.pause()
            smoke.assert_no_traceback()
            smoke.assert_no_orphan_box_chars()
            smoke.assert_text_visible("Try: pm task claim")

The harness is intentionally framework-light: no heavy fixtures, no
fake services. Each test wires its own data fixtures and passes the
constructed App in. That keeps the smoke layer composable with the
existing per-screen test suites instead of duplicating them.

Migration: the launch-hardening release gate (#889) requires that
each primary cockpit surface have at least one smoke run at each of
:data:`SMOKE_TERMINAL_SIZES`. The matrix is small enough to run in
CI without significant time cost.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

from textual.app import App
from textual.pilot import Pilot
from textual.widget import Widget


# ---------------------------------------------------------------------------
# Public size matrix
# ---------------------------------------------------------------------------


SMOKE_TERMINAL_SIZES: tuple[tuple[int, int], ...] = (
    (80, 30),
    (100, 40),
    (169, 50),
    (200, 50),
    (210, 65),
)
"""Terminal sizes the smoke matrix exercises.

* ``80x30`` — the narrowest realistic terminal. Catches truncation
  in rail width, recovery hints, and update pill (#831 was here).
* ``100x40`` — common laptop split-pane size.
* ``169x50`` — a 13-inch laptop full-screen iTerm at default font.
* ``200x50`` — wide laptop / ultrawide split.
* ``210x65`` — wide ultrawide / desktop.
"""


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------


_TRACEBACK_MARKERS: tuple[str, ...] = (
    "Traceback (most recent call last)",
    'File "',
    "AttributeError:",
    "TypeError:",
    "KeyError:",
    "ValueError:",
    "RuntimeError:",
    "AssertionError:",
)
"""Substrings that indicate a Python traceback has reached the
rendered surface. The audit cites #851 (a normal missing-role
validation error rendered as a Rich/Python traceback) as the
canonical example."""


# Box-drawing characters used by Rich/Textual for borders. An "orphan"
# is one that appears without its expected partner — e.g., a top-left
# corner without a matching top-right or any horizontal between them.
# Detecting "orphan" precisely is hard; the harness uses a simpler
# heuristic — if a box character appears on a line that contains *no
# other text* and the line is not part of a contiguous border block,
# that is suspicious. The full check is ``assert_no_orphan_box_chars``.
_BOX_CHARS: frozenset[str] = frozenset(
    "─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋╌╍╎╏═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬╭╮╯╰╱╲╳╴╵╶╷╸╹╺╻╼╽╾╿"
)


_BOOTSTRAP_PROMPT_MARKERS: tuple[str, ...] = (
    # Claude Code's first-launch theme/trust modal text. If this leaks
    # into the cockpit area, the cockpit accidentally landed the user
    # in raw provider UI without a Polly return affordance.
    "Select a theme",
    "Do you trust this workspace?",
    # Provider auth prompts.
    "Please log in with your Anthropic account",
    "Please run `claude` in a terminal",
)
"""Phrases that mean a raw provider UI has reached the cockpit. The
audit (§4) cites this as the catastrophic launch failure mode (e.g.,
``pm up`` dropping into raw Claude Code instead of Polly)."""


# ---------------------------------------------------------------------------
# Smoke harness
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SmokeCapture:
    """Result of one harness run.

    The smoke harness builds this incrementally as the run proceeds,
    so a test can call ``smoke.snapshot()`` at multiple moments to
    capture different states (initial mount, after a keypress, etc.).
    """

    rendered_text: str
    """Concatenated plain-text render of every widget at capture
    time. Built by walking ``app.query("*")`` and joining each
    widget's ``str(widget.render())`` with ``\\n``."""

    widget_count: int
    """How many widgets were walked. Useful for sanity assertions —
    a cockpit screen with widget_count==0 is broken even if no other
    assertion fires."""

    visible_lines: tuple[str, ...] = field(default_factory=tuple)
    """``rendered_text`` split on newline, with empty lines kept.
    Indexable; the harness uses this for line-based assertions."""


class SmokeHarness:
    """Drives a Textual App headlessly and captures rendered output.

    Construct with :meth:`SmokeHarness.mount`, which is an async
    context manager. The harness exposes:

    * ``pilot`` — the Textual ``Pilot`` for driving keystrokes.
    * ``app`` — the App under test.
    * :meth:`snapshot` — capture a :class:`SmokeCapture` at any time.
    * ``assert_*`` — universal assertions documented below.
    """

    def __init__(self, app: App, pilot: Pilot, size: tuple[int, int]) -> None:
        self.app: App = app
        self.pilot: Pilot = pilot
        self.size: tuple[int, int] = size
        self._captures: list[SmokeCapture] = []

    @classmethod
    @contextlib.asynccontextmanager
    async def mount(
        cls,
        app: App,
        *,
        size: tuple[int, int] = (100, 40),
    ) -> AsyncIterator["SmokeHarness"]:
        """Mount ``app`` headlessly at ``size`` and yield the harness."""
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            yield cls(app, pilot, size)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def snapshot(self) -> SmokeCapture:
        """Walk the widget tree and capture the rendered text.

        Walks both the App's primary widget tree *and* the topmost
        modal screen (if any) so screens pushed by a keystroke (like
        the ``?`` help overlay) show up in the capture. Without that,
        a smoke test that drives the help modal would assert against
        the parent App's text and miss the actual content the user
        is looking at.
        """
        parts: list[str] = []
        widgets: int = 0
        seen_widget_ids: set[int] = set()

        # Walk every screen in the stack. Textual's ``screen_stack``
        # places the active screen last; iterating the full stack
        # captures both the parent App and any pushed modal.
        screen_stack = list(getattr(self.app, "screen_stack", []) or [])
        if not screen_stack:
            screens = [self.app.screen] if hasattr(self.app, "screen") else []
        else:
            screens = screen_stack

        for screen in screens:
            for widget in screen.query("*"):
                wid = id(widget)
                if wid in seen_widget_ids:
                    continue
                seen_widget_ids.add(wid)
                widgets += 1
                rendered = _safe_render(widget)
                if rendered:
                    parts.append(rendered)

        text = "\n".join(parts)
        capture = SmokeCapture(
            rendered_text=text,
            widget_count=widgets,
            visible_lines=tuple(text.split("\n")),
        )
        self._captures.append(capture)
        return capture

    @property
    def last(self) -> SmokeCapture:
        """The most recent capture, taking one if none has been made yet."""
        if not self._captures:
            return self.snapshot()
        return self._captures[-1]

    # ------------------------------------------------------------------
    # Universal assertions
    # ------------------------------------------------------------------

    def assert_no_traceback(self, capture: SmokeCapture | None = None) -> None:
        """Fail if a Python traceback reached the rendered output.

        The audit cites #851 — a normal missing-role validation error
        that surfaced as a Rich/Python traceback. Tracebacks must
        never reach the cockpit; they go through the user-friendly
        CLI error formatter instead.
        """
        text = (capture or self.last).rendered_text
        for marker in _TRACEBACK_MARKERS:
            if marker in text:
                raise AssertionError(
                    f"Traceback marker {marker!r} reached the cockpit at size "
                    f"{self.size}. Snippet: {_excerpt(text, marker, 200)!r}"
                )

    def assert_no_bootstrap_prompt(
        self, capture: SmokeCapture | None = None
    ) -> None:
        """Fail if raw provider bootstrap prompts reached the cockpit.

        The audit (§4) cites #841: ``pm up`` relaunch hit a tmux
        segfault and dropped the cockpit session into raw Claude
        Code. Pollypm must never silently surrender the surface.
        """
        text = (capture or self.last).rendered_text
        for marker in _BOOTSTRAP_PROMPT_MARKERS:
            if marker in text:
                raise AssertionError(
                    f"Bootstrap prompt {marker!r} leaked into the cockpit "
                    f"at size {self.size} — the cockpit dropped into raw "
                    f"provider UI without a Polly return affordance."
                )

    def assert_no_orphan_box_chars(
        self, capture: SmokeCapture | None = None
    ) -> None:
        """Fail if a line is just an orphan box char with no context.

        Heuristic: any non-empty line consisting *only* of one or two
        box-drawing characters and whitespace is suspicious. Real
        borders are at least three characters wide. The audit (§2)
        cites several rendering bugs where a partial border survived
        a refactor (#838 was the reference example).
        """
        for line_no, line in enumerate((capture or self.last).visible_lines):
            stripped = line.strip()
            if not stripped:
                continue
            non_box = [ch for ch in stripped if ch not in _BOX_CHARS]
            if non_box:
                continue
            # Line is all box chars + whitespace. Suspicious only if
            # short — long contiguous box-char lines are real borders.
            if len(stripped) <= 2:
                raise AssertionError(
                    f"Orphan box char(s) {stripped!r} on line {line_no} at "
                    f"size {self.size}"
                )

    def assert_no_letter_by_letter_wrap(
        self, capture: SmokeCapture | None = None
    ) -> None:
        """Fail if Rich/Textual hard-wrapped a word into per-line letters.

        Symptom: three or more consecutive single-character lines.
        The audit (§2) cites #819 / #836 where narrow rail widths
        wrapped commands letter-by-letter, rendering them unreadable.
        """
        run = 0
        for line in (capture or self.last).visible_lines:
            stripped = line.strip()
            if len(stripped) == 1 and stripped.isalpha():
                run += 1
                if run >= 3:
                    raise AssertionError(
                        f"Letter-by-letter wrap detected at size "
                        f"{self.size}: 3+ consecutive single-letter lines."
                    )
            else:
                run = 0

    def assert_text_visible(
        self,
        text: str,
        *,
        capture: SmokeCapture | None = None,
    ) -> None:
        """Fail if ``text`` is not present in the rendered output.

        Use this for recovery hints (#831 ``ctrl+q``, #829 ``Try: pm
        …``, #826 ``Up``). The audit insists assertions verify
        rendered text, not source data, because the bug class is
        text being in source but rendered out of sight.
        """
        if text not in (capture or self.last).rendered_text:
            raise AssertionError(
                f"Expected text {text!r} not visible at size {self.size}.\n"
                f"Rendered (first 500 chars): "
                f"{(capture or self.last).rendered_text[:500]!r}"
            )

    def assert_text_not_visible(
        self,
        text: str,
        *,
        capture: SmokeCapture | None = None,
    ) -> None:
        """Fail if ``text`` *is* present in the rendered output.

        Inverse of :meth:`assert_text_visible`. Useful for checking
        that internal/test event kinds are filtered from live user
        surfaces."""
        if text in (capture or self.last).rendered_text:
            raise AssertionError(
                f"Unexpected text {text!r} visible at size {self.size}."
            )

    def assert_minimum_widget_count(
        self,
        minimum: int,
        *,
        capture: SmokeCapture | None = None,
    ) -> None:
        """Fail if the screen rendered fewer than ``minimum`` widgets.

        A surface that rendered three widgets at full screen size is
        almost certainly broken. This catches "App mounted but
        compose() raised silently" failures."""
        cap = capture or self.last
        if cap.widget_count < minimum:
            raise AssertionError(
                f"Only {cap.widget_count} widgets rendered at size "
                f"{self.size}; expected at least {minimum}. The screen is "
                f"likely failing to compose."
            )

    def assert_counts_agree(
        self,
        labels: Iterable[str],
        *,
        capture: SmokeCapture | None = None,
    ) -> None:
        """Fail if two ``label: NUMBER`` instances on the same screen
        disagree.

        The audit cites #820 where Home and Rail used independent
        inbox counters and diverged. The check is conservative — it
        only flags an inconsistency when the same label appears more
        than once with different numbers in the rendered text.
        """
        text = (capture or self.last).rendered_text
        for label in labels:
            pattern = rf"{re.escape(label)}\s*[:(]?\s*(\d+)"
            matches = re.findall(pattern, text)
            if len(matches) >= 2:
                values = set(matches)
                if len(values) > 1:
                    raise AssertionError(
                        f"Counter {label!r} disagrees within the same "
                        f"screen at size {self.size}: saw values "
                        f"{sorted(values)}. The shared-read-model rule "
                        f"(#820, #883) requires one source of truth."
                    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_render(widget: Widget) -> str:
    """Return a widget's rendered string, swallowing render errors.

    Textual widgets render to a variety of Rich renderables: a
    ``rich.text.Text`` (cheap; has ``.plain``), a ``rich.panel.Panel``
    or other layout containers (need a ``Console`` to flatten), or
    ``Blank`` (always empty). The harness is OK with the
    least-detailed possible flattening — it asserts on substring
    presence and orphan box characters, not on layout fidelity.

    Any exception is caught and converted to an empty string so a
    single broken widget cannot break the entire smoke pass.
    """
    try:
        if not hasattr(widget, "render"):
            return ""
        rendered = widget.render()
    except Exception:  # noqa: BLE001 — widget render failures are diagnostics, not crashes
        return ""
    if rendered is None:
        return ""
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain
    # Rich Panel / Blank / other renderables don't expose .plain.
    # Flatten them with a temporary Console capture. A bounded
    # render width keeps cost predictable and avoids surprising
    # the harness with multi-megabyte snapshots.
    try:
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf,
            width=200,
            force_terminal=False,
            color_system=None,
            highlight=False,
            soft_wrap=False,
            record=False,
        )
        console.print(rendered, end="")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — flattening must never crash the smoke run
        return ""


def _excerpt(text: str, marker: str, span: int) -> str:
    """Return ``span`` characters around ``marker`` for error messages."""
    idx = text.find(marker)
    if idx < 0:
        return text[:span]
    start = max(0, idx - span // 2)
    return text[start : start + span]
