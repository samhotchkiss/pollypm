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

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Iterable

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
        """Capture the rendered text the user actually sees.

        #898 — earlier the harness walked ``app.query("*")`` and
        flattened each widget's ``render()`` through a 200-column
        ``rich.Console``. That captured pre-layout source text
        and missed the very class of bugs the audit cares about:
        text exists in widgets but is clipped / hidden / wrapped
        differently at the configured terminal size.

        The fix is to read Textual's compositor strips at the
        configured size. ``compositor.render_strips()`` returns
        one ``Strip`` per terminal row, post-layout, post-clip.
        Each Strip's ``.text`` property is exactly what the user
        would see at that row.

        The harness still walks ``app.query("*")`` to count
        widgets — the count is used by
        :meth:`assert_minimum_widget_count` as a silent-compose
        guard. Visible text comes from the compositor.
        """
        widgets = 0
        seen_widget_ids: set[int] = set()
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

        # Compositor read — the active (top-of-stack) screen's
        # compositor is what the user sees. Strips that fail to
        # render fall back to the legacy widget-walk so a smoke
        # run never produces an empty capture purely because of
        # a Textual API change.
        strip_lines: list[str] = []
        active_screen = screens[-1] if screens else None
        compositor = getattr(active_screen, "_compositor", None) if active_screen else None
        if compositor is not None:
            try:
                strips = compositor.render_strips()
                for strip in strips:
                    text = getattr(strip, "text", None)
                    if isinstance(text, str):
                        strip_lines.append(text)
            except Exception:  # noqa: BLE001 — fall back to legacy walk
                strip_lines = []

        if not strip_lines:
            # Fallback path. Documented and tested explicitly so
            # callers know the harness can degrade gracefully on
            # an environment where the compositor is unavailable.
            # Use a fresh seen-set: the count walk above already
            # marked every widget, so reusing that set would make
            # the fallback walk a no-op.
            strip_lines = self._fallback_widget_walk(screens, set())

        text = "\n".join(strip_lines)
        capture = SmokeCapture(
            rendered_text=text,
            widget_count=widgets,
            visible_lines=tuple(strip_lines),
        )
        self._captures.append(capture)
        return capture

    def _fallback_widget_walk(
        self, screens, seen_widget_ids: set[int]
    ) -> list[str]:
        """Legacy widget-tree walk used when the compositor is
        unavailable. Documented as a fallback so tests can assert
        the path is reachable; production smoke runs use
        ``render_strips`` (the audit's #898 contract)."""
        parts: list[str] = []
        for screen in screens:
            for widget in screen.query("*"):
                wid = id(widget)
                if wid in seen_widget_ids:
                    continue
                seen_widget_ids.add(wid)
                rendered = _safe_render(widget)
                if rendered:
                    parts.append(rendered)
        text = "\n".join(parts)
        return text.split("\n") if text else []

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


# ---------------------------------------------------------------------------
# Smoke scenario registry + runner (#911)
# ---------------------------------------------------------------------------


SmokeScenarioBody = Callable[["SmokeHarness"], Awaitable[None]]
"""Type alias for the body of a smoke scenario.

The body receives a fully mounted :class:`SmokeHarness`, runs whatever
keystrokes/assertions it needs, and is expected to raise on any
rendering or assertion failure. The scenario runner translates
exceptions into :class:`SmokeFailure` records — it does not swallow
them silently."""


AppFactory = Callable[[], App]
"""A zero-arg callable that returns a fresh ``textual.app.App``.

Used by :class:`SmokeScenario` so each (scenario, size) cell of the
matrix gets a fresh App — Textual Apps are not reusable once
``run_test`` has exited."""


@dataclass(frozen=True, slots=True)
class SmokeScenario:
    """One scenario in the rendered smoke matrix.

    A scenario is the *minimum unit of rendered coverage* the release
    gate cares about. Each scenario:

    * names a target App via :attr:`app_factory` (zero-arg builder so
      every (scenario, size) cell gets a fresh App);
    * defines a :attr:`body` that mounts assertions on the rendered
      output;
    * declares which terminal sizes from the audit matrix it covers
      via :attr:`sizes` (defaults to the full :data:`SMOKE_TERMINAL_SIZES`).

    The scenario contract is intentionally tiny so the gate can run
    a representative subset directly inside the gate process. Heavy
    fixture-driven scenarios (per-screen seeded data, real workspace)
    live in ``tests/test_cockpit_smoke_render.py`` and run in CI.
    """

    name: str
    """Stable identifier (snake_case)."""

    app_factory: AppFactory
    """Builds a fresh App for each (scenario, size) cell."""

    body: SmokeScenarioBody
    """Async callable that mounts assertions on the harness."""

    sizes: tuple[tuple[int, int], ...] = SMOKE_TERMINAL_SIZES
    """Subset of :data:`SMOKE_TERMINAL_SIZES` this scenario covers."""


@dataclass(frozen=True, slots=True)
class SmokeFailure:
    """One scenario+size cell that did not render cleanly."""

    scenario: str
    size: tuple[int, int]
    error_type: str
    message: str

    def render(self) -> str:
        return (
            f"{self.scenario} @ {self.size[0]}x{self.size[1]}: "
            f"{self.error_type}: {self.message}"
        )


async def _baseline_scenario_body(smoke: "SmokeHarness") -> None:
    """The default scenario body: mount + universal asserts.

    This is the smallest possible rendered smoke check — it runs the
    universal assertion bundle on a baseline App. Its purpose is to
    prove that the gate is actually rendering at the configured size,
    not just inspecting harness shape.
    """
    smoke.snapshot()
    smoke.assert_no_traceback()
    smoke.assert_no_bootstrap_prompt()
    smoke.assert_no_orphan_box_chars()
    smoke.assert_no_letter_by_letter_wrap()
    smoke.assert_minimum_widget_count(1)
    smoke.assert_text_visible("PollyPM smoke baseline")


def _build_baseline_app() -> App:
    """Construct the default baseline smoke App.

    A self-contained Textual App that mounts a single Static widget
    with a known marker string. Imported lazily inside the function
    so :mod:`pollypm.cockpit_smoke` does not pay the import cost
    until a smoke run actually executes.
    """
    from textual.app import App as _App, ComposeResult
    from textual.widgets import Static

    class _BaselineSmokeApp(_App):
        CSS = "Static { height: 1; }"

        def compose(self) -> ComposeResult:  # type: ignore[override]
            yield Static("PollyPM smoke baseline", id="smoke-marker")

    return _BaselineSmokeApp()


SMOKE_SCENARIOS: tuple[SmokeScenario, ...] = (
    SmokeScenario(
        name="baseline_render",
        app_factory=_build_baseline_app,
        body=_baseline_scenario_body,
        # The narrow + the wide bookends are enough to prove the
        # rendered path executed at both extremes; the full matrix
        # is exercised by the test-suite parametrization.
        sizes=((80, 30), (210, 65)),
    ),
)
"""Default set of scenarios the release gate executes.

Kept deliberately small so the gate stays fast and self-contained.
The deeper matrix (per-screen, seeded data, scrollable overlays)
lives in ``tests/test_cockpit_smoke_render.py`` and runs in CI."""


async def _run_one_cell(
    scenario: SmokeScenario, size: tuple[int, int]
) -> SmokeFailure | None:
    """Mount one (scenario, size) cell and execute the body.

    Returns a :class:`SmokeFailure` on any exception, ``None`` on
    success. The mount itself is wrapped in the same exception
    handler so a factory or compose-time crash also surfaces as a
    cell failure rather than tearing down the whole matrix.
    """
    try:
        app = scenario.app_factory()
        async with SmokeHarness.mount(app, size=size) as smoke:
            await scenario.body(smoke)
    except Exception as exc:  # noqa: BLE001 — record + continue
        return SmokeFailure(
            scenario=scenario.name,
            size=size,
            error_type=type(exc).__name__,
            message=str(exc),
        )
    return None


async def _run_smoke_scenarios_async(
    scenarios: Iterable[SmokeScenario],
) -> tuple[SmokeFailure, ...]:
    """Async core of :func:`run_smoke_matrix`.

    Iterates every (scenario, size) cell sequentially. Sequential
    execution is intentional: Textual's headless run uses asyncio
    primitives that do not compose cleanly under
    :func:`asyncio.gather`, and the matrix is small enough that
    serial execution finishes in well under the gate's budget.
    """
    failures: list[SmokeFailure] = []
    for scenario in scenarios:
        for size in scenario.sizes:
            failure = await _run_one_cell(scenario, size)
            if failure is not None:
                failures.append(failure)
    return tuple(failures)


def run_smoke_matrix(
    scenarios: Iterable[SmokeScenario] | None = None,
) -> tuple[SmokeFailure, ...]:
    """Execute the smoke matrix and return any failures.

    The release gate calls this. If ``scenarios`` is None the default
    :data:`SMOKE_SCENARIOS` registry runs. Tests inject a custom list
    to prove the gate actually invokes the runner (a stubbed failing
    scenario must produce a failure record).

    The function uses :func:`asyncio.run` so the gate caller stays
    sync. If a caller is already inside an event loop (e.g., a test
    with ``pytest-asyncio``) it should call
    :func:`_run_smoke_scenarios_async` directly instead.
    """
    chosen = tuple(scenarios) if scenarios is not None else SMOKE_SCENARIOS
    if not chosen:
        return ()
    return asyncio.run(_run_smoke_scenarios_async(chosen))
