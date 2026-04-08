# PollyPM Issue Tracker

This is the default local issue-tracker workflow for substantial PollyPM projects.

## Purpose

Use this workflow when a project has enough moving parts that informal prompts are no longer enough. The tracker keeps work small, reviewable, and resumable.

## Folder State Machine

Issues live under `issues/` and move through these folders:

- `00-not-ready`
- `01-ready`
- `02-in-progress`
- `03-needs-review`
- `04-in-review`
- `05-completed`

## Role Split

- PA owns implementation.
- PM owns review and merge.

PA responsibilities:
- pick the next small issue
- move it to `02-in-progress`
- implement and test it
- move it to `03-needs-review`
- notify PM that review is needed

PM responsibilities:
- move issues to `04-in-review`
- review and validate the work
- request changes or move to `05-completed`
- merge when approval criteria are satisfied

## Queue Files

PollyPM initializes:

- `issues/instructions.md`
- `issues/notes.md`
- `issues/progress-log.md`
- `issues/.latest_issue_number`

The latest-number file is the canonical source of truth for the next issue number.

## Guidance

- Keep issues small, testable, and independently shippable.
- Prefer many small issues over a few large ones.
- Keep the project north star visible while executing issue-level work.
- Use PM to review drift, scope creep, and low-value loops.
