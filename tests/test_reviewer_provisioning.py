"""Tests for reviewer auto-provisioning (#1413).

Three load-bearing behaviours covered:

1. **Bootstrap sweep** — ``ensure_reviewer_sessions_for_tracked_projects``
   provisions a ``reviewer_<project>`` SessionConfig for every tracked
   project that doesn't already have one. Untracked projects are left
   alone (matches the cockpit visibility model — tracked projects are
   the ones the watchdog cares about).

2. **On-demand provision** — ``ensure_reviewer_session_for_project``
   creates a reviewer session when called for a project that lacks one,
   and emits a ``session.provisioned`` audit event so the watchdog
   (#1414) can quantify provisioning activity.

3. **Idempotency** — running the bootstrap sweep twice does NOT create
   two reviewer sessions for the same project. The second pass is a
   no-op.

The bootstrap-time test deliberately uses the real ``register_project``
+ ``create_worker_session`` helpers so we exercise the full config-write
path instead of a happy-path stub. Mock seams (``_spawn``-style
parameters) live in ``test_no_session_auto_recovery.py``; this file
proves the integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pollypm.recovery.reviewer_provisioning as rp
from pollypm.audit.log import EVENT_SESSION_PROVISIONED
from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    # ``ensure_project_scaffold`` will create ``.pollypm/`` under each
    # registered repo; pre-create it so the audit emit's per-project
    # log path exists for our assertions.
    (repo / ".pollypm").mkdir(parents=True, exist_ok=True)
    return repo


def _write_config_with_projects(
    tmp_path: Path,
    *,
    projects: dict[str, tuple[Path, bool]],
) -> Path:
    """Build a PollyPMConfig with one account and the given projects.

    ``projects`` maps project_key -> (path, tracked).
    """
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            workspace_root=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_1"),
        accounts={
            "claude_1": AccountConfig(
                name="claude_1",
                provider=ProviderKind.CLAUDE,
                email="c@example.com",
                home=tmp_path / ".pollypm/homes/claude_1",
            ),
        },
        sessions={},
        projects={
            key: KnownProject(
                key=key,
                path=path,
                name=key,
                persona_name="",
                kind=ProjectKind.FOLDER,
                tracked=tracked,
            )
            for key, (path, tracked) in projects.items()
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


# ---------------------------------------------------------------------------
# 1. Bootstrap sweep provisions a reviewer per tracked project
# ---------------------------------------------------------------------------


def test_bootstrap_sweep_provisions_reviewer_for_tracked_project(
    tmp_path: Path, monkeypatch
) -> None:
    """A tracked project with no reviewer session at boot gets one.

    Uses the real ``ensure_reviewer_session_for_project`` path (which
    routes through ``create_worker_session``) so we exercise the full
    config-write contract, not a stub. The launch step is suppressed
    via ``launch=False`` (the real bootstrap path delegates launch to
    ``_bootstrap_launches`` via ``plan_launches``)."""
    repo = _make_repo(tmp_path, "savethenovel")
    config_path = _write_config_with_projects(
        tmp_path, projects={"savethenovel": (repo, True)},
    )

    # Suppress the real worktree creation — this test exercises the
    # config-write idempotency contract, not git-worktree plumbing.
    # ``ensure_worktree`` is the only collaborator we don't want to run
    # for real (it shells out to git and writes under the repo).
    import pollypm.workers as workers_mod

    monkeypatch.setattr(workers_mod, "ensure_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(workers_mod, "detect_logged_in", lambda account: True)

    results = rp.ensure_reviewer_sessions_for_tracked_projects(
        config_path, launch=False,
    )

    # One project, one created session, detail names the session.
    assert len(results) == 1
    project_key, created, detail = results[0]
    assert project_key == "savethenovel"
    assert created is True
    assert detail.startswith("session=reviewer_savethenovel"), detail

    # The on-disk config now carries the reviewer session.
    from pollypm.config import load_config
    config = load_config(config_path)
    reviewer_sessions = [
        s for s in config.sessions.values()
        if s.role == "reviewer" and s.project == "savethenovel"
    ]
    assert len(reviewer_sessions) == 1
    assert reviewer_sessions[0].name == "reviewer_savethenovel"
    assert reviewer_sessions[0].window_name == "reviewer-savethenovel"


def test_bootstrap_sweep_skips_untracked_projects(
    tmp_path: Path, monkeypatch
) -> None:
    """Untracked projects are left alone — they opt out of the cockpit
    visibility model and shouldn't burn account routing on a session
    nobody monitors."""
    repo_tracked = _make_repo(tmp_path, "tracked_proj")
    repo_untracked = _make_repo(tmp_path, "untracked_proj")
    config_path = _write_config_with_projects(
        tmp_path,
        projects={
            "tracked_proj": (repo_tracked, True),
            "untracked_proj": (repo_untracked, False),
        },
    )

    import pollypm.workers as workers_mod
    monkeypatch.setattr(workers_mod, "ensure_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(workers_mod, "detect_logged_in", lambda account: True)

    results = rp.ensure_reviewer_sessions_for_tracked_projects(
        config_path, launch=False,
    )

    by_project = {key: (created, detail) for key, created, detail in results}
    assert by_project["tracked_proj"][0] is True
    assert by_project["untracked_proj"] == (False, "not_tracked")

    from pollypm.config import load_config
    config = load_config(config_path)
    reviewer_for_untracked = [
        s for s in config.sessions.values()
        if s.role == "reviewer" and s.project == "untracked_proj"
    ]
    assert reviewer_for_untracked == [], (
        "Untracked projects must not get a reviewer session"
    )


# ---------------------------------------------------------------------------
# 2. On-demand provisioning fires when a task hits code_review
# ---------------------------------------------------------------------------


def test_on_demand_provision_creates_session_and_emits_audit_event(
    tmp_path: Path, monkeypatch
) -> None:
    """A task hitting ``code_review`` with no reviewer session triggers
    a fresh provision and emits a ``session.provisioned`` audit event
    with role/project/reason metadata so the watchdog (#1414) can
    surface provisioning activity."""
    repo = _make_repo(tmp_path, "demo")
    config_path = _write_config_with_projects(
        tmp_path, projects={"demo": (repo, True)},
    )

    import pollypm.workers as workers_mod
    monkeypatch.setattr(workers_mod, "ensure_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(workers_mod, "detect_logged_in", lambda account: True)

    # Capture emitted audit events so we can assert on shape.
    captured: list[dict[str, Any]] = []

    def fake_emit(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("pollypm.audit.emit", fake_emit)

    created, detail = rp.ensure_reviewer_session_for_project(
        config_path, "demo", launch=False,
    )
    assert created is True
    assert detail == "session=reviewer_demo"

    # Audit event was emitted with the right shape.
    provisioned_events = [
        e for e in captured if e.get("event") == EVENT_SESSION_PROVISIONED
    ]
    assert len(provisioned_events) == 1
    event = provisioned_events[0]
    assert event["project"] == "demo"
    assert event["status"] == "ok"
    assert event["subject"] == "reviewer_demo"
    assert event["metadata"]["role"] == "reviewer"
    assert event["metadata"]["project"] == "demo"
    # On-demand reason — the default for ensure_reviewer_session_for_project.
    assert event["metadata"]["reason"] == rp.REASON_ON_DEMAND_REVIEW


def test_on_demand_provision_idempotent_when_session_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """A second call for the same project is a no-op."""
    repo = _make_repo(tmp_path, "demo")
    config_path = _write_config_with_projects(
        tmp_path, projects={"demo": (repo, True)},
    )

    import pollypm.workers as workers_mod
    monkeypatch.setattr(workers_mod, "ensure_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(workers_mod, "detect_logged_in", lambda account: True)

    first_created, _ = rp.ensure_reviewer_session_for_project(
        config_path, "demo", launch=False,
    )
    assert first_created is True

    second_created, second_detail = rp.ensure_reviewer_session_for_project(
        config_path, "demo", launch=False,
    )
    assert second_created is False
    assert second_detail.startswith("already_exists:")

    # Still exactly one reviewer session in config.
    from pollypm.config import load_config
    config = load_config(config_path)
    reviewer_sessions = [
        s for s in config.sessions.values()
        if s.role == "reviewer" and s.project == "demo"
    ]
    assert len(reviewer_sessions) == 1


# ---------------------------------------------------------------------------
# 3. Idempotent bootstrap — running twice doesn't create duplicates
# ---------------------------------------------------------------------------


def test_bootstrap_sweep_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Running the bootstrap sweep twice doesn't double-provision.

    This is the load-bearing guard against the failure mode where a
    cockpit restart re-runs ``bootstrap_tmux`` and accidentally adds a
    second ``reviewer_<project>_2`` session to the config. The second
    pass must be a pure no-op."""
    repo = _make_repo(tmp_path, "savethenovel")
    config_path = _write_config_with_projects(
        tmp_path, projects={"savethenovel": (repo, True)},
    )

    import pollypm.workers as workers_mod
    monkeypatch.setattr(workers_mod, "ensure_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(workers_mod, "detect_logged_in", lambda account: True)

    first = rp.ensure_reviewer_sessions_for_tracked_projects(
        config_path, launch=False,
    )
    second = rp.ensure_reviewer_sessions_for_tracked_projects(
        config_path, launch=False,
    )

    # First pass created the session, second pass is a no-op.
    assert first[0][1] is True, first
    assert second[0][1] is False, second
    assert second[0][2].startswith("already_exists:"), second

    # Exactly one reviewer session in config after both passes.
    from pollypm.config import load_config
    config = load_config(config_path)
    reviewer_sessions = [
        s for s in config.sessions.values()
        if s.role == "reviewer" and s.project == "savethenovel"
    ]
    assert len(reviewer_sessions) == 1
