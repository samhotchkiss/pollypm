"""Post-port guard for #1790 — CLI is backend-aware.

History
-------

Slice K-tests part 5 (#1737, commit 04285152) deleted 15 CLI test
modules because the CLI surfaces themselves still hard-wired to
``SQLAlchemyStore(f"sqlite:///{db_path}")`` and the part-4..N spec
disallowed production-code edits in the same slice.

PR #1920 added the predecessor of this module as a *tripwire* — it
asserted the status quo (sqlite-only constructions still present) so
the suite would fail loudly the moment the port lifted that
precondition.

This iteration flips the assertion. The CLI has ported (commit at the
top of #1790's PR), so this module's job is now to *guard* against
regressions:

1. ``src/pollypm/work/inbox_cli.py`` MUST NOT construct
   ``SQLAlchemyStore(f"sqlite:///...")`` directly. All messages-table
   reads / writes should go through
   :func:`pollypm.work.inbox_cli._resolve_messages_store` (which
   honours ``[storage].backend``).
2. ``src/pollypm/cli_features/session_runtime.py`` MUST NOT construct
   ``SQLAlchemyStore(f"sqlite:///...")`` directly. ``pm notify`` writes
   route through :func:`pollypm.cli_features.session_runtime._resolve_notify_store`.

A failure means a future edit re-introduced the sqlite-only failure
mode (#1755 / #1811) that #1790 was filed to close.

Why keep this after the port shipped
------------------------------------

A reviewer looking at a one-line diff that re-adds
``SQLAlchemyStore(f"sqlite:///{db}")`` won't necessarily remember the
post-#1790 contract. The grep-style assertion catches the regression
at PR-test time instead of at runtime when a pg-backed deployment
loses notify visibility.
"""

from __future__ import annotations


_SQLITE_STORE_CONSTRUCTOR = 'SQLAlchemyStore(f"sqlite:///'


def test_inbox_cli_is_backend_aware() -> None:
    """``src/pollypm/work/inbox_cli.py`` must not pin sqlite directly.

    Use :func:`pollypm.work.inbox_cli._resolve_messages_store` (added
    for #1790) instead. It mirrors :func:`pollypm.work.cli._svc`
    dispatch: ``--db`` overrides force sqlite at the supplied path;
    the canonical default routes through
    :func:`pollypm.store.get_store` so ``[storage].backend`` decides.
    """
    from pathlib import Path

    import pollypm.work.inbox_cli as inbox_cli

    source = Path(inbox_cli.__file__).read_text(encoding="utf-8")
    occurrences = source.count(_SQLITE_STORE_CONSTRUCTOR)
    assert occurrences == 0, (
        "src/pollypm/work/inbox_cli.py reintroduced a direct "
        f"{_SQLITE_STORE_CONSTRUCTOR!r}... construction. The CLI is "
        "post-#1790 backend-aware — route every messages-table read / "
        "write through ``_resolve_messages_store(db)`` so "
        "``[storage].backend = 'postgres'`` deployments don't silently "
        "read the empty sqlite shadow (the #1755 / #1811 failure mode)."
    )


def test_cli_features_session_runtime_is_backend_aware() -> None:
    """``src/pollypm/cli_features/session_runtime.py`` must not pin sqlite directly.

    Use :func:`pollypm.cli_features.session_runtime._resolve_notify_store`
    (added for #1790). ``pm notify`` and the immediate-priority inbox-task
    fan-out share a single backend-aware Store singleton so the message
    write + the task creation land on the same DB.
    """
    from pathlib import Path

    import pollypm.cli_features.session_runtime as session_runtime

    source = Path(session_runtime.__file__).read_text(encoding="utf-8")
    occurrences = source.count(_SQLITE_STORE_CONSTRUCTOR)
    assert occurrences == 0, (
        "src/pollypm/cli_features/session_runtime.py reintroduced a "
        f"direct {_SQLITE_STORE_CONSTRUCTOR!r}... construction. "
        "Post-#1790 ``pm notify`` writes route through "
        "``_resolve_notify_store(db)`` so the messages-store write and "
        "the work-service task creation share the same backend."
    )
