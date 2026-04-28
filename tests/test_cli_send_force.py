"""Tests for the ``pm send`` worker-gate behaviour (#261).

The default blocks ``pm send`` to worker sessions, nudging the operator
toward ``pm task create``. When the auto-pickup path is broken,
``--force`` is the escape hatch that lets the operator nudge a worker
directly. These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from pollypm.cli import app


runner = CliRunner()


class _FakeSessionCfg:
    def __init__(self, role: str, project: str = "demo") -> None:
        self.role = role
        self.project = project


def _make_supervisor(sessions: dict) -> MagicMock:
    supervisor = MagicMock()
    supervisor.config.sessions = sessions
    supervisor.send_input = MagicMock()
    return supervisor


def test_send_to_worker_without_force_is_blocked(tmp_path: Path) -> None:
    """Default path: sending to a worker session exits non-zero with guidance."""
    supervisor = _make_supervisor({"worker_demo": _FakeSessionCfg("worker")})
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(app, ["send", "worker_demo", "hello"])
    assert result.exit_code == 1
    assert "Blocked: dispatch work through the task system" in result.stdout
    assert "--force" in result.stdout
    supervisor.send_input.assert_not_called()


def test_send_to_worker_with_force_is_allowed(tmp_path: Path) -> None:
    """Escape hatch: --force bypasses the worker-role gate."""
    supervisor = _make_supervisor({"worker_demo": _FakeSessionCfg("worker")})
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(app, ["send", "worker_demo", "hello", "--force"])
    assert result.exit_code == 0, result.stdout
    supervisor.send_input.assert_called_once()
    args, kwargs = supervisor.send_input.call_args
    assert kwargs.get("force") is True


def test_send_to_non_worker_is_not_gated() -> None:
    """pm send to operator/reviewer/heartbeat is unchanged — gate only fires for workers."""
    supervisor = _make_supervisor({"pm-operator": _FakeSessionCfg("operator-pm")})
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(app, ["send", "pm-operator", "hello"])
    assert result.exit_code == 0, result.stdout
    supervisor.send_input.assert_called_once()


def test_send_to_unknown_session_is_not_gated() -> None:
    """Unknown session names fall through; the downstream send_input call decides."""
    supervisor = _make_supervisor({})
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(app, ["send", "ghost_session", "hello"])
    assert result.exit_code == 0, result.stdout
    supervisor.send_input.assert_called_once()


def test_send_project_slash_number_shortcut_resolves_to_task_window() -> None:
    """``pm send <project>/<N>`` translates to ``task-<project>-<N>`` (#924).

    The shortcut keeps users from having to type the full per-task
    window name. The CLI rewrites the argument before passing it on to
    ``send_input``, which is what reaches the storage closet.
    """
    supervisor = _make_supervisor({})
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(
            app, ["send", "blackjack-trainer/6", "use the new helper", "--owner", "human"],
        )
    assert result.exit_code == 0, result.stdout
    supervisor.send_input.assert_called_once()
    args, kwargs = supervisor.send_input.call_args
    # First positional is the resolved session name.
    assert args[0] == "task-blackjack-trainer-6"
    assert args[1] == "use the new helper"


def test_send_unknown_session_surfaces_friendly_error() -> None:
    """A KeyError from ``launch_by_session`` surfaces as a CLI BadParameter (#924).

    Pre-#924 the underlying ``KeyError`` propagated as a stack trace.
    The CLI now catches it and surfaces the planner's friendly message
    listing valid configured sessions and pointing at ``pm task next``.
    """
    supervisor = _make_supervisor({})
    supervisor.send_input.side_effect = KeyError(
        "Unknown session: ghost. Valid configured sessions: operator. "
        "For per-task workers use ``pm task next``..."
    )
    with patch("pollypm.cli._load_supervisor", return_value=supervisor):
        result = runner.invoke(app, ["send", "ghost", "hello"])
    assert result.exit_code != 0
    # Friendly text from the planner is preserved through Typer's
    # BadParameter wrapper.
    assert "Unknown session: ghost" in result.stderr or "Unknown session: ghost" in result.stdout
    assert "pm task next" in result.stderr or "pm task next" in result.stdout
