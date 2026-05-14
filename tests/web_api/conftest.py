"""Shared fixtures for the Web API integration tests.

Each fixture builds a tmp ``PollyPMConfig`` whose work-DB path
points at a per-test SQLite file, registers a single project, and
wires the bearer-auth token into a tmp file. The FastAPI app is
constructed via :func:`pollypm.web_api.create_app` so the tests
exercise the same code path ``pm serve`` uses at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Filesystem root for the test project (with .pollypm/ scaffold)."""
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def api_config(tmp_path: Path, project_root: Path, workspace_root: Path) -> PollyPMConfig:
    """A minimal :class:`PollyPMConfig` with one tracked project."""
    base_dir = workspace_root / ".pollypm"
    state_db = base_dir / "state.db"
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=state_db,
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "codex_primary",
            ),
        },
        sessions={},
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
    )
    return config


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "api-token"


@pytest.fixture
def token(token_path: Path) -> str:
    value, _generated = ensure_token(token_path)
    return value


@pytest.fixture
def app(api_config, token_path, token):  # noqa: ARG001 — token fixture must run
    return create_app(config=api_config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``POLLYPM_AUDIT_HOME`` so test events don't bleed."""
    audit = tmp_path / "audit"
    audit.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit))
    return audit


def make_task(svc, *, project: str = "myproj", title: str = "Task", **kwargs):
    """Build + persist a task through the work service."""
    defaults = dict(
        title=title,
        description=kwargs.pop("description", ""),
        type=kwargs.pop("type", "task"),
        project=project,
        flow_template=kwargs.pop("flow_template", "standard"),
        roles=kwargs.pop("roles", {"worker": "agent-1", "reviewer": "agent-2"}),
        priority=kwargs.pop("priority", "normal"),
        created_by=kwargs.pop("created_by", "tester"),
    )
    defaults.update(kwargs)
    return svc.create(**defaults)
