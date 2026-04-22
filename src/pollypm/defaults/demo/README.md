# PollyPM Demo Repo

This repo is the offline fallback used by onboarding when no recent local
projects are detected.

It is intentionally small, but self-contained:

- `demo_app.py` contains a tiny queue-summary helper with one deliberate bug.
- `tests/test_demo_app.py` contains the matching failing regression test.
- `TASK.md` describes the seeded PollyPM task that onboarding can add to the
  project database.

## What To Try

- Run `python -m unittest discover -s tests -p "test_*.py"`
- Read `demo_app.py`
- Fix the queue estimate regression, then rerun the demo tests
- Ask Polly to explain, extend, or test the queue summary behavior

Everything in this repo uses only the Python standard library.
