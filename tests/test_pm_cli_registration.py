"""Smoke test that catches typer-introspection regressions on command callbacks.

Issue #1500: ``functools.partial`` wrappers on Typer command callbacks tripped
``typing.get_type_hints`` (Python 3.13's typing.py raises ``TypeError`` because
partials don't satisfy "module/class/method/function"). The whole pm CLI
became non-functional on every fresh install. This test re-registers the CLI
in a clean Typer app and exercises the same introspection path Typer takes
internally — any future regression that breaks ``inspect.signature`` /
``get_type_hints`` on a registered command surfaces here at unit-test speed.
"""

from __future__ import annotations

import inspect
import typing

import typer
from typer.testing import CliRunner

import pollypm.cli  # noqa: F401  — import alone must not raise
from pollypm.cli_features.session_runtime import (
    _bind_session_command,
    launch,
    rail_daemon,
    register_session_runtime_commands,
    reset,
    status,
)


def test_session_runtime_commands_register_without_typer_introspection_error():
    """Registering and invoking ``--help`` on the CLI must not raise.

    This is the same path ``pm --help`` walks at runtime; a regression that
    makes the registered callbacks un-introspectable surfaces here.
    """
    app = typer.Typer()
    register_session_runtime_commands(app, helpers=pollypm.cli)

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output


def test_bound_session_callback_is_real_function_not_partial():
    """``_bind_session_command`` must return a real function so that
    ``typing.get_type_hints`` (which Typer calls internally) succeeds.
    """
    bound = _bind_session_command(launch, helpers=pollypm.cli)
    assert inspect.isfunction(bound), (
        f"Expected a real function for typer introspection; got {type(bound)!r}"
    )

    # The smoking gun: this is exactly what typer's get_params_from_function
    # invokes. It used to raise on functools.partial wrappers.
    hints = typing.get_type_hints(bound)
    assert isinstance(hints, dict)


def test_bound_session_callback_drops_helpers_parameter_from_signature():
    """The bound wrapper must hide the first ``helpers`` parameter from typer
    so that the user-facing signature only shows the typer options.
    """
    bound = _bind_session_command(launch, helpers=pollypm.cli)
    sig = inspect.signature(bound)
    # ``launch`` declares ``helpers`` as its first parameter; the wrapper
    # binds it. Only the typer options/arguments should remain visible.
    assert "helpers" not in sig.parameters
    # ``launch`` has a ``config_path`` typer option — that should still be
    # exposed on the bound wrapper.
    assert "config_path" in sig.parameters


def test_bound_session_callback_invokes_underlying_callback_with_helpers():
    """When invoked, the wrapper must prepend the bound ``helpers`` so the
    real callback sees its first argument.
    """
    captured = {}

    def fake_callback(helpers, x: int = 0) -> int:
        captured["helpers"] = helpers
        captured["x"] = x
        return x * 2

    sentinel_helpers = object()
    bound = _bind_session_command(fake_callback, helpers=sentinel_helpers)
    result = bound(x=5)
    assert result == 10
    assert captured["helpers"] is sentinel_helpers
    assert captured["x"] == 5


def test_session_runtime_commands_each_have_introspectable_signature():
    """Every command in ``register_session_runtime_commands`` should pass
    ``typing.get_type_hints`` after binding — a generalized form of the
    #1500 regression check.
    """
    for callback in (launch, rail_daemon, reset, status):
        bound = _bind_session_command(callback, helpers=pollypm.cli)
        hints = typing.get_type_hints(bound)
        assert isinstance(hints, dict), (
            f"get_type_hints failed on bound {callback.__name__!r}"
        )
