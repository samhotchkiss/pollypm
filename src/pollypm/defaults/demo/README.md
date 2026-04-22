# PollyPM Demo Repo

This repo is the offline fallback used by onboarding when no recent local
projects are detected.

It is intentionally small, but self-contained:

- The shipped repo is a 12-file mini project with a replayable git history.

- `demo_app.py` contains a tiny queue-summary helper with one deliberate bug.
- `tests/test_demo_app.py` contains the matching failing regression test.
- `demo_cli.py` is a tiny entrypoint that prints the demo queue summary.
- `demo_data.py` holds sample task titles used by the CLI and tests.
- `demo_history.md` explains the replayable git history seeded by onboarding.
- `TASK.md` describes the seeded PollyPM task that onboarding can add to the
  project database.

## What To Try

- Run `python -m unittest discover -s tests -p "test_*.py"`
- Read `TASK.md` and `demo_history.md`
- Fix the queue estimate regression, then rerun the demo tests
- Ask Polly to explain, extend, or test the queue summary behavior

Everything in this repo uses only the Python standard library.
