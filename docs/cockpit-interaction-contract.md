# Cockpit Interaction Contract

Source: implements GitHub issue #881.

This document defines the runtime contract every cockpit surface
(Home, Rail, Inbox, Activity, Settings, Tasks, Project Dashboard,
PM Chat, Help) must follow. The contract has a code home at
`src/pollypm/cockpit_interaction.py` and a test home at
`tests/test_cockpit_interaction_contract.py`.

The pre-#881 audit (`docs/launch-issue-audit-2026-04-27.md` §1)
identified 26 cockpit-input/focus issues in the last 500 GitHub
issues. The deepest example was #840: a fix protected approve/reject
shortcuts only while an `Input` had focus, but a smoke test still
approved a real task because the Tasks view opened with table focus.
That class of bug is structural — it cannot be solved by another
per-screen patch. The contract below is the structural fix.

## Vocabulary

* **Surface** — a top-level cockpit App or route the user can land
  on directly (Home, Rail, Inbox, Activity, Settings, Tasks,
  Project Dashboard, PM Chat, Help). Modals are not surfaces.
* **Binding** — a key-to-action declaration. The Textual `Binding`
  class is wrapped by `CockpitBinding`, which adds two fields the
  contract needs: `scope` and `kind`.
* **Scope** — where a binding is allowed to fire.
  `GLOBAL`/`APP`/`SEARCH`/`MODAL`. See `BindingScope`.
* **Kind** — the risk class of an action.
  `NAVIGATION`/`READ`/`WRITE`/`DESTRUCTIVE`. See `ActionKind`.
* **Arming** — the two-press confirmation pattern for destructive
  single-key actions. First press arms; a second press of the same
  key on the same target within `ARM_WINDOW_SECONDS` confirms.

## Rules

### 1. Initial focus is declared, not implicit

Every surface declares its `initial_focus` (`FocusKind.TABLE`,
`LIST`, `INPUT`, `CUSTOM`, `NONE`) in its `ScreenContract`. A
surface that has a visible text input *and* destructive single-key
bindings must set `arming_required_for_destructive=True`. The audit
in `InteractionRegistry.audit()` rejects any other combination.

The Tasks surface opens with `DataTable` focus. Single-letter
`a`/`x` (approve/reject) are destructive. The screen has a visible
search `Input`. Therefore: arming is required.

### 2. Destructive actions in input-bearing surfaces require arming

Any single-key destructive action declared on a surface with
`has_visible_input=True` must run through
`destructive_action_safe(app, action, target_id=…, selection=…)`.
The helper returns `True` only when:

1. **Explicit selection** — the target id is in the screen's
   selection set (e.g., `_selected_task_ids`). The user opted in by
   pressing `space` to toggle.
2. **Confirmation press** — the same key was pressed on the same
   target within `ARM_WINDOW_SECONDS` (default 3s). The first press
   arms and surfaces a hint; the second press fires.

It returns `False` (and arms, if rule 2 is the path) when:

* a text `Input` currently has focus — the keystroke belongs to
  the input, not to the destructive handler;
* this is the first press without an explicit selection.

The legacy guard `if isinstance(self.focused, Input): return` is
still acceptable on surfaces that have *no* visible text input
(e.g., the cockpit rail, the metrics drill-down modal). Surfaces
with a visible input must use `destructive_action_safe` instead.

### 3. Modal trapping suspends APP-scope bindings

When a `ModalScreen` is on top, the cockpit suspends every
`BindingScope.APP` binding for the duration of the modal. Only
`BindingScope.GLOBAL` bindings (`Ctrl+Q`, `Ctrl+W`, `?`,
`Ctrl+K`/`:`) and the modal's own `BindingScope.MODAL` bindings
fire. The audit rejects any modal that declares `BindingScope.APP`
bindings.

### 4. Search mode is exclusive

When a search `Input` has focus, single-letter `BindingScope.APP`
keys route to the input. `Escape` exits search and yields focus
back to the table. `Enter` submits and yields focus back. Global
bindings still fire.

This is enforced two ways:

* **At the binding layer**: surfaces with a search input mark
  destructive APP bindings as requiring arming (rule 2). If the
  user is typing, no Input-focus check is needed because the input
  consumes the key.
* **At the action layer**: action handlers call
  `destructive_action_safe` first thing. If `Input` has focus, the
  helper returns `False` and the action returns immediately.

### 5. Escape never silently consumes

`Escape` follows a deterministic priority:

1. If a modal is on top → close the modal.
2. If search has focus → exit search.
3. If the surface is a detail App → return to home.
4. If the surface is the home/rail App → no-op (do *not* quit).

Surfaces are forbidden from binding `Escape` to a custom action
that violates this priority order.

### 6. Quit policy

* `Ctrl+Q` is the only universal quit. It is the only
  `BindingScope.GLOBAL`-scoped binding that mutates app state.
* `q`/`Q` on the rail is also quit (the rail is the top-level
  view, so local-back and quit are the same action).
* `q`/`Q` on every other surface is local-back: pop to home.
* `Ctrl+W` detaches the tmux client without killing the cockpit.

### 7. Help text source-of-truth

The keyboard-help modal renders the bindings returned by
`help_text_for_app(app)`. That function walks the active App's
runtime `BINDINGS` plus `GLOBAL_REFERENCE_BINDINGS`. The modal is
forbidden from maintaining its own copy of binding labels.

`docs/launch-issue-audit-2026-04-27.md` cites two recurring help
bugs (#831, #826) where data was correct but the rendered help
diverged from runtime behavior. Sourcing help from runtime
`BINDINGS` makes that class of drift impossible.

### 8. Project Dashboard first-touch states are explicit

The Project Dashboard is the first cockpit surface a user lands in
after selecting a project from the rail. It must not render as a bare
glyph or empty body when there is no executable work yet.

For a project whose path exists and whose task counts total zero across
all `work_tasks` status buckets, the dashboard renders the
`#proj-empty-state` affordance at the top of `#proj-body`, before the
inbox/action sections. The panel is inline, not a modal. It tells the
operator that the project has no tasks and names the two available
ways to move it forward:

* `pm project plan <project>` — start the architect/planning flow.
* `pm task create` — add a task manually.

That empty-state affordance ends as soon as any task row exists in any
bucket. It is also suppressed when the project path is missing on disk,
because the topbar owns that louder missing-project state.

Draft work is also part of the dashboard contract. A task in
`work_status="draft"` is visible in the Task pipeline strip and row
groups; drafts are not allowed to disappear merely because they are not
yet `queued`. When `draft > 0` and there is no live work
(`queued + in_progress + review == 0`), the dashboard additionally
renders `#proj-drafts-pending` directly below the empty-state slot. The
panel shows the number of pending drafts, lists up to three
`<project>/<number>` refs, and points at `pm task queue <id>` as the
promotion path from draft to queued.

`#proj-drafts-pending` hides when there are no drafts, when any live
work exists, or when the project path is missing. In mixed states,
where drafts coexist with queued/in-progress/review tasks, the pipeline
draft bucket is the visible surface and the standalone panel stays
hidden to avoid duplicating the same signal.

The drafts panel does not introduce a new APP binding. The promotion
action is the existing work-service transition exposed by
`pm task queue <id>`. If a future cockpit action or keybinding promotes
drafts directly, that PR must update the runtime `BINDINGS`,
`ScreenContract`, and `help_text_for_app(app)` output together; this
document should continue to describe the state contract instead of
copying a separate keymap.

## Migration

The contract is enforced for surfaces that register a
`ScreenContract` with `REGISTRY`. The cockpit surfaces register
their contracts at the bottom of their app-class module so the
audit fires on import. Surfaces that have not yet registered are
not required to comply, but the launch-hardening release gate
(#889) blocks v1 if the registered surface set does not at least
include Tasks (the #840 origin) and any other surface with a
visible text input.

When a new surface is added, its contract registration is part of
the same PR. The CI smoke test
(`tests/test_cockpit_interaction_contract.py::test_audit_clean`)
asserts `REGISTRY.audit() == []` for all registered contracts.

## Appendix: full keymap reference

The canonical list of `BindingScope.GLOBAL` keys lives in
`GLOBAL_REFERENCE_BINDINGS`. As of 2026-04-27 it is:

| Key       | Action                  | Kind       |
| --------- | ----------------------- | ---------- |
| `Ctrl+K`  | open_command_palette    | READ       |
| `:`       | open_command_palette    | READ       |
| `?`       | show_keyboard_help      | READ       |
| `Ctrl+Q`  | request_quit            | WRITE      |
| `Ctrl+W`  | detach                  | WRITE      |

Per-surface bindings are declared in each App's `BINDINGS` list and
mirrored into the registered `ScreenContract`. Use
`help_text_for_app(app)` at runtime; do not duplicate the list.

*Last updated: 2026-05-07.*
