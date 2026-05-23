"""Production guard: ``register_backend("sqlite", ...)`` must hard-fail.

Issue #1970 / sqlite-ripout sequence (refs #1971). The reviewer flagged
several production paths that re-registered the sqlite backend
mid-process, re-enabling the split-brain sqlite-shadow class of bugs
even after a stock install fails ``[storage].backend = "sqlite"`` at
:func:`pollypm.store.get_store`.

PRs #1977 and follow-ups removed every live ``register_backend("sqlite",
...)`` caller from production. This test pins the remaining surface
shut: if a future commit re-introduces such a call in a non-pytest
process, :func:`pollypm.store.registry.register_backend` now raises
:class:`ValueError` instead of merely logging a warning.

Two complementary assertions:

1. **Pytest opt-in still works.** Tests legitimately need to register
   the legacy sqlite factory for fixtures that haven't migrated yet;
   the ``_running_under_pytest()`` short-circuit keeps that path open.
2. **Production rejection bites.** When ``_running_under_pytest()``
   returns ``False`` (simulated via monkeypatch), the same call raises
   :class:`ValueError` with a message naming the issue tags.

If either assertion regresses, the sqlite-ripout guard described in
issue #1970's "Suggested fix" has been weakened — restore the hard
rejection in :func:`pollypm.store.registry.register_backend`.
"""

from __future__ import annotations

import pytest

from pollypm.store.registry import register_backend, unregister_backend


def _dummy_factory(*, url: str) -> object:  # pragma: no cover - never called
    """Stand-in factory; never invoked because the guard fires first."""
    raise AssertionError(
        "dummy factory should never be constructed — the guard runs "
        "before the registry mutation."
    )


def test_register_backend_sqlite_allowed_under_pytest() -> None:
    """The pytest opt-in path still registers sqlite without raising.

    ``_running_under_pytest()`` is True for this test, so the guard
    short-circuits and the registry mutation completes normally.
    Clean up after ourselves via :func:`unregister_backend` so the
    test never leaves a sqlite mapping behind for the next test.
    """
    try:
        register_backend("sqlite", _dummy_factory)
    finally:
        unregister_backend("sqlite")


def test_register_backend_sqlite_rejected_outside_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-path rejection: ``register_backend("sqlite", ...)`` raises.

    Force ``_running_under_pytest`` to return False so the function
    sees the same world a stock production process does. The call
    must raise :class:`ValueError` and the registry must remain
    untouched (so we don't leak the dummy factory).
    """
    from pollypm.store import registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "_running_under_pytest", lambda: False,
    )

    with pytest.raises(ValueError) as exc_info:
        register_backend("sqlite", _dummy_factory)

    message = str(exc_info.value)
    assert "sqlite" in message.lower(), (
        "rejection message must name the sqlite backend so operators "
        "see which call site tripped the guard."
    )
    assert "#1971" in message or "#1970" in message, (
        "rejection message must cite the sqlite-ripout issue tags "
        "(#1971 / #1970) so the operator can find the rationale."
    )

    # Registry must remain free of the dummy factory: the guard fires
    # BEFORE the mutation, so a rejected production call has no
    # observable side effect.
    assert "sqlite" not in registry_mod._REGISTERED_BACKENDS, (
        "register_backend must reject sqlite BEFORE mutating the "
        "registry — otherwise a caller catching the ValueError still "
        "ends up with sqlite installed process-wide."
    )


def test_register_backend_non_sqlite_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is sqlite-specific — other backends register normally.

    Non-sqlite names (e.g. third-party experimental backends) must
    continue to register even outside pytest. The guard is narrow on
    purpose: the whole point of issue #1970 is to keep sqlite out, not
    to lock down the registry entirely.
    """
    from pollypm.store import registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "_running_under_pytest", lambda: False,
    )

    backend_name = "_test_registry_guard_dummy"
    try:
        register_backend(backend_name, _dummy_factory)
        assert backend_name in registry_mod._REGISTERED_BACKENDS, (
            "non-sqlite backends must still register on production paths."
        )
    finally:
        unregister_backend(backend_name)


def test_register_backend_sqlite_quiet_opt_in_outside_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``quiet=True`` opt-in remains available for legacy migrations.

    A one-off legacy migration tool that genuinely needs sqlite can
    pass ``quiet=True`` to acknowledge the opt-in. The docstring
    promises this escape hatch; pin it here so a future tightening
    doesn't break the migration path silently.
    """
    from pollypm.store import registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "_running_under_pytest", lambda: False,
    )

    try:
        register_backend("sqlite", _dummy_factory, quiet=True)
        assert "sqlite" in registry_mod._REGISTERED_BACKENDS, (
            "quiet=True must remain an explicit migration opt-in."
        )
    finally:
        unregister_backend("sqlite")
