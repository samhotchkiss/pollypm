# PollyPM Overnight Test Report
**Date:** April 14-15, 2026
**Duration:** ~4 hours
**Iterations:** 11

## Summary

PollyPM managed 30 projects and completed 64+ tasks overnight through its
fully autonomous task lifecycle. All work flowed through Polly (operator),
per-task workers (Claude Code in isolated worktrees), and Russell (reviewer).

## Scale

| Metric | Count |
|--------|-------|
| Projects managed | 30 |
| Tasks completed | 64+ |
| Tasks tonight (new) | 34+ |
| Projects fully completed | 16 |
| Rejections (real issues) | 6 |
| PollyPM test suite | 885 passing |

## Projects Built Tonight

### Fully Completed (16)
| Project | Tasks | Type | Highlights |
|---------|-------|------|------------|
| weather-cli | 5 | Standard + User-review | Working CLI: city search, forecast, colors, units |
| todo-api | 3 | Dependency chain | 30 passing tests, full CRUD |
| shortlink | 5 | 5-task dependency chain | All auto-unblocked sequentially |
| commit-validator | 3 | Dependency + rejection | Build-backend caught by Russell |
| md-render | 3 | Dependency chain | Parser, renderer, CLI all working |
| camptown | 2 | Standard + rejection | 3 rejection cycles, approved on real fix |
| puzzle-solver | 1 | Standard | 8-queens: 92 correct solutions verified |
| polly-report | 1 | Standard | Status report generator |
| data-viz | 1 | Standard | Terminal bar chart renderer |
| tablefmt | 1 | Standard | Markdown table formatter |
| color-palette | 1 | Standard | Color harmony generator |
| mini-calc | 1 | Spike (no review) | Research task |
| file-sorter | 1 | Spike (no review) | Research task |
| link-checker | 1 | Bug flow | Reproduce, fix, review |
| passgen | 1 | Standard | Password generator |
| pollypm-docs | 1 | User-review | Getting started guide, human approved |

## Flow Types Exercised

- **Standard** (implement, code_review, done): 20+ tasks
- **Spike** (research, done — no review): 2 tasks
- **Bug** (reproduce, fix, code_review, done): 1 task
- **User-review** (implement, human_review, done): 2 tasks
- **Cancellation**: 1 task (weather-cli/6)
- **Hold/resume**: 1 task (weather-cli/5)

## Rejection Cycle

6 rejections, all caught real issues:

1. **camptown/2** — Missed docs/project-overview.md references (rejected 2x, approved on v3 with real fix)
2. **md2html/1** — Invalid pyproject.toml build-backend (rejected 2x)
3. **commit-validator/1** — Same build-backend issue (rejected 2x)

Russell never approved unfixed rework.

## Dependency Chains

| Project | Chain | Result |
|---------|-------|--------|
| shortlink | 5 tasks | All auto-unblocked on approval |
| todo-api | 3 tasks | All auto-unblocked |
| commit-validator | 3 tasks | All auto-unblocked |
| md-render | 3 tasks | All auto-unblocked |

## Working Software Verified

- **WeatherCLI**: `uv run python -m weathercli --city NYC` — real weather data
- **TodoAPI**: 30 passing pytest tests, full CRUD
- **MD-Render**: `echo "# Hello" | uv run python -m md_render` — styled terminal output
- **PuzzleSolver**: 92 correct 8-queens solutions
- **TableFmt**: `echo '[{"name":"Alice","age":30}]' | uv run python -m tablefmt` — markdown tables

## Fixes Made During Testing

1. Dashboard redesign with task overview and attention items
2. Rail project grouping (active first, alphabetical)
3. Rail coloring (yellow for active tasks)
4. Per-task worker sessions visible in rail
5. Human approval UX (a/x keybindings)
6. Review nudge excluding human_review tasks
7. Rework worker spawning on rejection
8. Input bar submission via tmux paste buffer
9. PollyDashboardApp crash fix
10. Heartbeat excluding task-* windows
11. Auth recovery same-account retry
12. All pm send references replaced with task system
