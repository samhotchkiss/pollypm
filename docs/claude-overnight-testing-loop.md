# Claude Overnight Release-Hardening Prompt

You are working in `/Users/sam/dev/pollypm` on the `main` branch. Your job is to run an overnight testing and hardening loop for PollyPM’s v1 release readiness. You have permission to inspect the app as an actual user, identify rough edges, implement root-cause fixes, run tests, and push regular small commits.

## Operating Principles

- Keep going persistently. Do not stop after one pass if there are still useful improvements to make.
- Work in small, safe increments. Prefer one coherent fix per commit.
- Commit and push frequently. Do not batch hours of work into one large commit.
- Do not manually push product tasks forward to make the UI look better. Fix the root cause in PollyPM.
- Preserve plugin architecture boundaries. Do not reach into plugin internals from the cockpit/UI unless a public contract exists or you add one deliberately.
- Treat user-facing UX as the source of truth. Internal state can be complex, but the UI must clearly answer what is happening and what the user should do next.
- Avoid destructive git commands. Do not use `git reset --hard`, `git checkout --`, or revert user work unless explicitly instructed.
- If you find unrelated dirty files, do not overwrite them. Inspect first, then either work around them or make a small commit only for your own changes.

## High-Level Product Goal

Make the project dashboard, inbox, task flow, and cockpit rail genuinely useful for a non-technical operator trying to understand:

1. Is this project moving?
2. If not, who or what is it waiting on?
3. What exactly can I do right now to move it forward?
4. Where can I click to take that action?

The dashboard should be a control surface, not a dump of internal task/status data.

## Current Important Context

Recent relevant commits:

- `075f965` `Make project dashboards action oriented`
- `5654c2a` `Add dashboard burn-in loop`

Key recent changes:

- Project dashboards now have a state banner: moving, waiting on user, blocked by dependencies, paused, queued, or clear.
- Action Needed cards now render plain-language user prompts and contextual buttons.
- `pm notify` supports `--user-prompt-json`, which is intended to be the contract for user-facing copy and buttons.
- Plan review cards should show `Review plan` and `Open task`, not generic approve/wait actions.
- Dashboard overview sections are clickable.
- The old Codex burn-in loop was stopped. You may start your own persistent loop.

## Primary Surfaces To Inspect

Inspect these as a curious, skeptical user. Ask: “Why is this here? Is this actionable? Is this accurate? Can I click it? Does it tell me what to do?”

### 1. Project Dashboard

Command examples:

```bash
uv run pm cockpit-pane project polly_remote --config ~/.pollypm/pollypm.toml
uv run pm cockpit-pane project booktalk --config ~/.pollypm/pollypm.toml
```

Check:

- Top banner states the real project state in plain English.
- Yellow means user attention is needed or work is paused, not a false red alert.
- Red is reserved for true operational alert/fault states, not normal idle or dependency waiting.
- If user action is needed, the Action Needed section clearly says what to do.
- If no user action is needed, the dashboard should not imply that the user must act.
- Action cards do not expose obscure node names, task codes, or hidden reviewer jargon unless unavoidable.
- Action card buttons match the context.
- Clicking cards/sections routes somewhere useful.
- “Current activity” explains either active work or why there is no active work.
- “Task pipeline” is navigational and understandable.
- “Plan” and “Recent activity” are discoverable but not treated as urgent notifications.

Specific cases to inspect:

- `Notesy` / `polly_remote`: should explain deployment/live environment blockers in clear setup steps.
- `BookTalk`: should clearly indicate that the user needs to review the project plan and offer `Review plan`.
- `Health Coach` or other idle projects: should not show an alert if nothing is actually blocked or broken.

### 2. Inbox

Command examples:

```bash
uv run pm cockpit-pane inbox --config ~/.pollypm/pollypm.toml
uv run pm inbox
```

Check:

- Inbox is an action queue, not an undifferentiated stream.
- It hides or clearly separates FYI/dev/orphaned/deleted-project messages.
- Items requiring user action are obvious.
- A task assigned to the user should be surfaced as an inbox item.
- Plan review items show plan-review affordances.
- The message text should make sense without hidden context.
- Clicking or pressing the obvious keys should route to the right task/project/thread.

### 3. Task Flow

Check that tasks cannot fall through the cracks. Every non-terminal task should clearly be one of:

- actively in progress with an assigned active worker,
- waiting for a worker slot,
- in review / waiting for review,
- waiting for user action,
- blocked by dependency,
- paused/on hold with a clear reason,
- terminal.

Look for:

- rejected review tasks not returning to queue/work.
- tasks in `on_hold` with no visible owner/action.
- tasks that say “review feedback” or similar but are not actionable.
- projects with no active agents but no explanation.
- sessions disappearing from rail while still visible in right pane.

### 4. Cockpit Rail / Tmux Behavior

Use the live tmux session carefully:

```bash
tmux list-sessions
tmux list-panes -t pollypm:PollyPM -F '#{pane_id} #{pane_index} #{pane_current_command} #{pane_width}x#{pane_height}'
tmux capture-pane -t pollypm:PollyPM.0 -p -S 0
```

Check:

- Launching `pm` should not repeatedly show an upgrade from `rc2 -> rc2`.
- `q`, `w`, and `ctrl-q` behavior should be sane in rail/right pane contexts.
- Mouse clicks should work where UI implies clickability.
- Left rail worker/session visibility should match actual session state.
- Worker sessions should not be removed from the rail just because work is waiting for acceptance/integration.
- The right pane should not cycle through unrelated sessions on launch.

### 5. Waiting-On-User Contract

The intended contract is:

- Whenever a PM/architect/reviewer places work in a user-waiting state, it must provide a structured user-facing prompt.
- The prompt must include plain summary, concrete steps, decision question, and contextual actions/buttons.
- The dashboard should render that contract when present.
- Heuristics are fallback only for old/bad messages.

Current CLI support:

```bash
pm notify "..." "..." --priority immediate --user-prompt-json '{...}'
```

Inspect and improve:

- `src/pollypm/cli_features/session_runtime.py`
- `src/pollypm/cockpit_ui.py`
- `src/pollypm/plugins_builtin/project_planning/profiles/architect.md`
- reviewer/operator/worker prompts that still tell agents to send raw `pm notify` text without the user prompt contract.

## Suggested Testing Loop

Run this as a persistent loop, but do not blindly spam commits. Each cycle should inspect, decide, implement, test, commit, push.

### Cycle Steps

1. Pull/confirm current branch state.

```bash
git status --short
git pull --ff-only origin main
```

2. Run focused tests and invariants.

```bash
uv run pytest -q \
  tests/test_project_dashboard_ui.py \
  tests/test_cockpit_rail_routes.py \
  tests/test_plan_review_flow.py \
  tests/test_notification_tiering.py

uv run python scripts/release_invariants.py --config ~/.pollypm/pollypm.toml
```

3. Render real UI states via tmux and capture output.

```bash
tmux new-session -d -s claude-notesy-dashboard -x 210 -y 70 \
  'cd /Users/sam/dev/pollypm && uv run pm cockpit-pane project polly_remote --config ~/.pollypm/pollypm.toml'

tmux new-session -d -s claude-booktalk-dashboard -x 210 -y 70 \
  'cd /Users/sam/dev/pollypm && uv run pm cockpit-pane project booktalk --config ~/.pollypm/pollypm.toml'

sleep 2
tmux capture-pane -t claude-notesy-dashboard:0.0 -p -S 0 -E 70
tmux capture-pane -t claude-booktalk-dashboard:0.0 -p -S 0 -E 70
```

4. Read the UI like a user. Identify one highest-value rough edge.

5. Implement the smallest root-cause fix.

6. Add or update focused tests for the behavior.

7. Run:

```bash
uv run python -m py_compile src/pollypm/cockpit_ui.py src/pollypm/cli_features/session_runtime.py
uv run pytest -q <focused tests>
uv run python scripts/release_invariants.py --config ~/.pollypm/pollypm.toml
git diff --check
```

8. Commit and push:

```bash
git status --short
git add <changed files>
git commit -m "<small, descriptive message>"
git push origin main
```

9. Repeat.

## Areas Likely Worth Improving

Prioritize based on live inspection, but these are known likely rough edges:

- Action Needed cards for two simultaneous actions may be too tall/noisy.
- “Other open items” may still duplicate the main action and create confusion.
- Buttons may record replies but not always drive the underlying task transition strongly enough.
- “Review plan” currently routes to inbox; it may be more useful to route to the plan review item directly if there is a stable route.
- The top banner might over-prioritize a lower-priority action if multiple action items exist.
- Some old messages still lack `user_prompt`, so fallback heuristics may produce acceptable but imperfect copy.
- Existing role prompts beyond the architect prompt may still need the `user_prompt` contract.
- Release invariants probably need more checks around “waiting on user but no user prompt contract.”
- Projects marked active because an architect session exists may still be semantically waiting on user. The banner should prioritize user action over active architect presence, which it mostly does now; verify.
- It may be useful to show “No user action needed” explicitly for idle/blocked dependency states.
- The rail may still treat worker visibility differently from project acceptance/integration state.

## What Not To Do

- Do not manually approve/reject/resume production tasks just to make dashboards green.
- Do not delete or close real user messages unless the product behavior requires it and tests cover it.
- Do not make a massive refactor during the overnight loop.
- Do not bypass the plugin architecture by hardcoding plugin-specific internals into global UI without a contract.
- Do not leave failing tests or unpushed commits.
- Do not rely only on unit tests; always inspect at least one live tmux-rendered surface per cycle.

## Reporting Format

At the end of each cycle, write a short note in the tmux session:

```text
Cycle <n> complete:
- inspected: <surfaces>
- fixed: <thing>
- tests: <commands/results>
- pushed: <commit sha>
- next: <next likely rough edge>
```

If a cycle finds no worthwhile code change, still record what was inspected and why no change was made, then continue to another surface.

