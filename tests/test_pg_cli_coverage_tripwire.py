"""Tripwire for the deferred CLI coverage port (#1790).

Slice K-tests part 5 (#1737, commit 04285152) deleted 15 CLI test
modules. The deletion was deliberate: the CLI surfaces themselves
still hard-wire to ``SQLAlchemyStore("sqlite:///{db_path}")``, and
the part-4..N spec disallows production-code edits in the same slice.

Deleted in part 5 — these all need to come back once the CLI ports
to pg:

* ``tests/test_cli_bug_report.py``
* ``tests/test_cli_inbox_backfill.py``
* ``tests/test_cli_notify.py``
* ``tests/test_inbox_aggregator_workspace_root.py``
* ``tests/test_inbox_dedup.py``
* ``tests/test_inbox_fake_recovery_injection_gate.py``
* ``tests/test_inbox_messages_reader.py``
* ``tests/test_inbox_show_msg.py``
* ``tests/test_inbox_sweep.py``
* ``tests/test_inbox_view.py``
* ``tests/test_notification_tiering.py``
* ``tests/test_rail_badge_awaits_user.py``
* ``tests/test_work_cli.py``
* ``tests/test_work_hold_resume_regressions.py``
* ``tests/test_work_task_tokens.py``

What this file does
-------------------

The tests below are **tripwires**, not coverage. They assert the
status quo of the blocker: when the CLI ports to pg, the assertions
fail loudly, prompting whoever ships the port to re-add the deleted
test suites against a pg-aware ``--db`` resolver + ``pg_cli_runner``
fixture (the harness sketch in #1790's action-items list).

Why a tripwire instead of an xfail or a skip
--------------------------------------------

* ``pytest.skip`` would silently drop off the dashboard.
* ``xfail`` would pass when the gap closed, with no nudge to port the
  real coverage.
* A real tripwire fails the suite when the precondition lifts, which
  is the *exact* signal we want: "you ported the CLI, now port the
  tests."
"""

from __future__ import annotations


def test_inbox_cli_still_hard_wired_to_sqlite():
    """``src/pollypm/work/inbox_cli.py`` still constructs SQLAlchemyStore.

    When this assertion fails:

    1. The CLI module no longer hard-wires to sqlite.
    2. Add the ``pg_cli_runner`` fixture to ``tests/conftest_pg.py``:
       provisions a per-test schema via ``pg_schema_pool``, returns a
       typer runner whose env routes ``--db`` to the test schema's DSN.
    3. Restore the 15 deleted test modules (see the file docstring for
       the full list) and rebind their ``--db <state.db>`` calls to
       the new runner.
    4. Delete this tripwire — its job is done.

    The check counts ``SQLAlchemyStore`` occurrences in
    ``src/pollypm/work/inbox_cli.py``. The expected value is the
    snapshot at the time the test was written; any decrease means the
    port is in flight and the deleted CLI tests need to come back.
    """
    from pathlib import Path

    import pollypm.work.inbox_cli as inbox_cli

    source = Path(inbox_cli.__file__).read_text(encoding="utf-8")
    occurrences = source.count("SQLAlchemyStore")
    assert occurrences > 0, (
        "src/pollypm/work/inbox_cli.py no longer references "
        "SQLAlchemyStore — the CLI has ported off sqlite. Time to "
        "re-add the deleted CLI test modules (see this file's "
        "docstring for the list) and delete this tripwire (#1790)."
    )


def test_cli_features_session_runtime_still_hard_wired_to_sqlite():
    """``src/pollypm/cli_features/session_runtime.py`` still uses
    ``SQLAlchemyStore("sqlite:///{db_path}")``.

    When this assertion fails the ``test_cli_notify.py`` /
    ``test_notification_tiering.py`` families can be ported alongside
    the inbox CLI tests — those suites exercised the notification
    paths owned by ``session_runtime.py``.
    """
    from pathlib import Path

    import pollypm.cli_features.session_runtime as session_runtime

    source = Path(session_runtime.__file__).read_text(encoding="utf-8")
    occurrences = source.count('SQLAlchemyStore(f"sqlite:///')
    assert occurrences > 0, (
        "src/pollypm/cli_features/session_runtime.py no longer "
        "constructs SQLAlchemyStore with a sqlite URL — the CLI "
        "notification path has ported off sqlite. Time to re-add the "
        "deleted notify / tiering tests (#1790)."
    )
