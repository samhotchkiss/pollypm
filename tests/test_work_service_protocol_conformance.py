"""Conformance test for the :class:`WorkService` protocol (#796).

Locks the published protocol to the same set of keyword arguments
that the concrete ``PgWorkService`` and ``MockWorkService``
implementations accept on the methods CLI/runtime callers exercise.
The pre-fix protocol omitted ``skip_gates`` (queue/claim/node_done/
approve), ``created_by`` (create), and ``entry_type`` (add_context/
get_context) — a third-party service satisfying the protocol could
silently reject those calls. This test is the contract that keeps
the surface honest going forward.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from pollypm.work.mock_service import MockWorkService
from pollypm.work.pg_service import PgWorkService
from pollypm.work.service import WorkService


# Methods + the parameter names that must appear in the protocol AND
# in every shipped concrete implementation. Update both sides at once
# when adding a new optional flag.
_REQUIRED_PARAMETERS: dict[str, set[str]] = {
    # ``kind`` (#1565) is part of the create contract so producers can
    # stamp the structured inbox-item discriminator at task creation
    # time without reaching past the work-service boundary.
    "create": {"created_by", "priority", "description", "kind"},
    "queue": {"skip_gates"},
    "claim": {"skip_gates"},
    "node_done": {"skip_gates"},
    "approve": {"skip_gates"},
    "add_context": {"entry_type"},
    "get_context": {"entry_type"},
}


def _params(cls: type, name: str) -> set[str]:
    return set(inspect.signature(getattr(cls, name)).parameters)


@pytest.mark.parametrize(
    "method_name, expected_params", sorted(_REQUIRED_PARAMETERS.items()),
)
def test_protocol_accepts_required_parameters(
    method_name: str, expected_params: set[str],
) -> None:
    """The published protocol must declare every shared parameter."""
    proto_params = _params(WorkService, method_name)
    missing = expected_params - proto_params
    assert not missing, (
        f"WorkService.{method_name} missing parameters {missing}; "
        "protocol is narrower than the contract callers depend on."
    )


@pytest.mark.parametrize(
    "impl_cls", [PgWorkService, MockWorkService],
)
@pytest.mark.parametrize(
    "method_name, expected_params", sorted(_REQUIRED_PARAMETERS.items()),
)
def test_implementations_accept_required_parameters(
    impl_cls: type, method_name: str, expected_params: set[str],
) -> None:
    """PgWorkService + MockWorkService must accept every shared parameter."""
    impl_params = _params(impl_cls, method_name)
    missing = expected_params - impl_params
    assert not missing, (
        f"{impl_cls.__name__}.{method_name} missing parameters {missing}; "
        "implementation is narrower than the published protocol."
    )


def test_concrete_impls_carry_every_protocol_method() -> None:
    """``PgWorkService`` and ``MockWorkService`` must implement every
    method named on ``WorkService`` — guards regressions where a method
    is removed from one impl but not the other. Plain ``isinstance``
    won't work because ``WorkService`` isn't ``@runtime_checkable``.
    """
    proto_methods = {
        name for name, value in inspect.getmembers(WorkService)
        if not name.startswith("_") and callable(value)
    }
    for impl_cls in (PgWorkService, MockWorkService):
        impl_methods = {
            name for name, value in inspect.getmembers(impl_cls)
            if not name.startswith("_") and callable(value)
        }
        missing = proto_methods - impl_methods
        assert not missing, (
            f"{impl_cls.__name__} missing protocol methods: {sorted(missing)}"
        )
