# Rendered Cockpit Smoke Harness

Source: implements GitHub issue #882.

This document specifies the rendered cockpit smoke harness — the
launch-hardening test layer that asserts on what the user *sees*,
not on what the helper functions *return*. Code home:
`src/pollypm/cockpit_smoke.py`. Test home:
`tests/test_cockpit_smoke_render.py`.

## Why a separate harness layer

The pre-launch audit (`docs/launch-issue-audit-2026-04-27.md` §2)
cites a class of bug helper-level unit tests cannot catch:

* `#831` — the upgrade pill shortened correctly in the data, but
  the rendered text still hid `ctrl+q` in the default 30-column
  rail.
* `#829` — preserving `Try: pm task claim` in the activity row
  data did not help: the rendered Activity column still cut the
  command off.
* `#826` — the help modal data was correct, but scroll/page
  boundaries made the `Up` label unreachable.

Every one of those bugs would have passed a unit test that
asserted against the helper's return value. They reproduce only
when an actual Textual widget tree is mounted at a realistic
terminal size and walked for rendered text.

The smoke harness is the structural fix: a thin reusable layer
that mounts the App, drives a short keystroke script, walks the
widget tree, and runs a small set of universal assertions. It
does *not* replace the per-screen test suites — those still own
deep behavior coverage. The harness owns the rendered-text
assertions everyone else was missing.

## The size matrix

`SMOKE_TERMINAL_SIZES` is the canonical list:

| Size      | Why                                                 |
| --------- | --------------------------------------------------- |
| `80x30`   | Narrowest realistic terminal — the #831 case        |
| `100x40`  | Common laptop split-pane                            |
| `169x50`  | 13" laptop full-screen iTerm at default font size   |
| `200x50`  | Wide laptop / ultrawide split                       |
| `210x65`  | Wide ultrawide / desktop                            |

The set is small enough to be feasible in CI per surface
(currently five sizes × four surfaces × ~1s each ≈ 20s total) and
broad enough to cover both extreme widths.

## Universal assertions

Every smoke test runs the universal bundle:

* `assert_no_traceback` — fails if any of `_TRACEBACK_MARKERS`
  appears in the rendered text. The audit cites #851 (a missing-
  role validation that surfaced as a Rich/Python traceback).
* `assert_no_bootstrap_prompt` — fails if raw provider UI text
  (Claude theme/trust modal, "please log in") leaks into the
  cockpit. The audit (§4) cites #841 — `pm up` segfaulted tmux
  and dropped the cockpit into raw Claude Code without a return
  affordance. The cockpit must never silently surrender the
  surface.
* `assert_no_orphan_box_chars` — fails if a non-empty line is
  one or two box-drawing characters with no other content. Real
  borders are at least three characters wide; a 1-2 char box-only
  line is almost always leftover from an unfinished refactor.
* `assert_no_letter_by_letter_wrap` — fails on three or more
  consecutive single-letter lines. The audit (§2) cites #819 /
  #836 where narrow rail widths wrapped commands letter-by-letter.
* `assert_minimum_widget_count(3)` — fails if the screen rendered
  fewer than three widgets. A real cockpit screen always has at
  least header + body + status; one or zero widgets means
  `compose()` raised silently.

## Per-surface assertions

Beyond the universal bundle, each surface adds checks for its own
recovery affordances. Examples:

* `keyboard_help_modal` — `assert_text_visible("ctrl+q")` so the
  global quit hint cannot be hidden behind a scroll boundary.
* (When wired) `inbox_app` — `assert_counts_agree(["Inbox"])` so
  the Home and Rail counts cannot diverge again (#820).
* (When wired) `activity_feed_app` — `assert_text_visible("Try:
  pm")` for any row that emits a recovery command (#829).

## What the harness does *not* test

* Live tmux sessions / worker panes — those are PM Chat/raw
  provider surfaces. They are tested at the integration layer
  (#884 launch state machine) where a real tmux client and
  pane-pattern classifier are in scope.
* Visual/typographic styling — the harness flattens to plain text
  and asserts on substrings. SVG snapshot diff was deliberately
  rejected because pixel-stable golden files have a long tail of
  flake from font/Rich version drift.
* Full keystroke coverage — the cockpit interaction-contract
  smoke (#881) owns the keystroke matrix; the rendered smoke owns
  the post-render assertions.

## Adding a new screen to the matrix

1. Make sure the screen has a stable headless mount path. If it
   needs live tmux, it does not belong here — see `#884` instead.
2. Add a `@pytest.mark.parametrize("size", SMOKE_TERMINAL_SIZES)`
   test in `tests/test_cockpit_smoke_render.py`. Construct the App
   from the `smoke_env` fixture.
3. Run the universal smoke bundle via `_run_universal_smoke`.
4. Add per-surface custom assertions for the screen's specific
   recovery hints.
5. Update this document with the new surface row.

## Integration with the release gate

The launch-hardening release verification gate (#889) requires:

* Every primary cockpit surface in the smoke matrix passes at
  every size in `SMOKE_TERMINAL_SIZES`.
* The harness's own self-tests pass (the harness must not flake
  silently).

A smoke run failure is a P0 launch blocker.

*Last updated: 2026-04-27.*
