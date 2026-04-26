"""Plural agreement on the "still used by N session(s)" guards.

Cycle 118: ``_validate_account_removal`` (accounts.py) and
``remove_project`` (projects.py) hard-pluralised the noun in their
``BadParameter`` messages, so a user removing an account with a
single session ref read ``Account X is still used by sessions: foo``.
Match the noun to the count.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pollypm.accounts import _validate_account_removal
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
    KnownProject,
)
from pollypm.projects import remove_project
from pollypm.config import write_config


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            tmux_session="pollypm",
            workspace_root=tmp_path,
            base_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            snapshots_dir=tmp_path / "snapshots",
            state_db=tmp_path / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                email="user@example.com",
                home=tmp_path / "home_primary",
            ),
            "claude_secondary": AccountConfig(
                name="claude_secondary",
                provider=ProviderKind.CLAUDE,
                email="other@example.com",
                home=tmp_path / "home_secondary",
            ),
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                name="Demo",
                path=tmp_path / "demo",
                kind=ProjectKind.GIT,
            ),
        },
    )


def test_validate_account_removal_singular_session_ref(tmp_path: Path) -> None:
    """One session referencing the account → ``session: <name>`` (no s)."""
    cfg = _config(tmp_path)
    cfg.sessions["solo"] = SessionConfig(
        name="solo",
        role="worker",
        provider=ProviderKind.CLAUDE,
        account="claude_secondary",
        cwd=tmp_path,
        project="demo",
        window_name="solo",
    )
    with pytest.raises(typer.BadParameter) as excinfo:
        _validate_account_removal(cfg, "claude_secondary")
    msg = str(excinfo.value.message)
    assert "is still used by session: solo" in msg
    assert "sessions: solo" not in msg


def test_validate_account_removal_plural_session_refs(tmp_path: Path) -> None:
    """Multiple sessions → ``sessions: a, b``."""
    cfg = _config(tmp_path)
    for name in ("a", "b"):
        cfg.sessions[name] = SessionConfig(
            name=name,
            role="worker",
            provider=ProviderKind.CLAUDE,
            account="claude_secondary",
            cwd=tmp_path,
            project="demo",
            window_name=name,
        )
    with pytest.raises(typer.BadParameter) as excinfo:
        _validate_account_removal(cfg, "claude_secondary")
    msg = str(excinfo.value.message)
    assert "is still used by sessions:" in msg
    assert "session: " not in msg.replace("sessions:", "")


def test_remove_project_pluralises_session_word(tmp_path: Path) -> None:
    """Same shape: removing a project that still has one referencing
    session reads ``session: name``, not ``sessions: name``."""
    cfg = _config(tmp_path)
    cfg.sessions["only"] = SessionConfig(
        name="only",
        role="worker",
        provider=ProviderKind.CLAUDE,
        account="claude_primary",
        cwd=tmp_path,
        project="demo",
        window_name="only",
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(cfg, config_path, force=True)
    with pytest.raises(typer.BadParameter) as excinfo:
        remove_project(config_path, "demo")
    msg = str(excinfo.value.message)
    assert "is still used by session: only" in msg
    assert "sessions: only" not in msg
