"""Non-default store backends (issue #343).

This subpackage collects storage backends that don't ship as the
PollyPM default — they exist so the ``Store`` protocol can be proven
dialect-agnostic before a real implementation lands. The built-in
SQLite/SQLAlchemy backend lives one directory up in
:mod:`pollypm.store.sqlalchemy_store`; it's registered by the
``pollypm.store_backend`` entry-point group in the project's
``pyproject.toml`` and is what :func:`pollypm.store.registry.get_store`
returns by default.

Third-party backends — e.g. the future ``pollypm-store-postgres``
package — register themselves under the same entry-point group and get
picked up automatically by the registry. Nothing under
:mod:`pollypm.store.backends` is registered in PollyPM's own
``pyproject.toml``; these are **reference stubs only**.
"""
