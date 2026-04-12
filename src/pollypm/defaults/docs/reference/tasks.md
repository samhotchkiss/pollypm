# Task Management

PollyPM tracks work through an issue pipeline with 6 states. Issues can live on disk (file backend) or in GitHub Issues (github backend).

## Issue States

```
00-not-ready → 01-ready → 02-in-progress → 03-needs-review → 04-in-review → 05-completed
```

- **00-not-ready** — drafted but not actionable
- **01-ready** — ready for a worker to pick up
- **02-in-progress** — assigned to a worker, being executed
- **03-needs-review** — worker finished, waiting for PM review
- **04-in-review** — PM is actively reviewing
- **05-completed** — done

Backward moves are allowed (rework). Skipping states logs a warning and is rejected in strict mode.

## File Backend

Issues are markdown files in `<project>/issues/<state>/`. Moving an issue = moving the file between directories.

```
issues/
  00-not-ready/
  01-ready/0019-github-issue-system.md
  02-in-progress/
  03-needs-review/
  04-in-review/
  05-completed/0001-account-recovery.md
```

## GitHub Backend

Issues use `polly:*` labels for state. Configure per-project in `.pollypm/config/project.toml`:

```toml
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "owner/repo"
```

The GitHub backend auto-closes issues on completion and reopens on rework.

## Workflow

1. PM (operator) creates an issue and places it in `01-ready`
2. PM assigns it to a worker via `pm send worker_pollypm "Read issue 0027..."`
3. Worker executes, commits, moves issue to `03-needs-review`
4. PM reviews. If good → `05-completed`. If not → back to `02-in-progress` with rework notes.
