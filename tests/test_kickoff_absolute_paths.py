"""Tests for issue #263: control-prompt kickoff must use absolute paths.

Context: After #260 made kickoffs actually reach workers, the kickoff
text contained relative paths like ``control-prompts/worker_<name>.md``
and ``.pollypm/docs/SYSTEM.md``. Workers run with a cwd inside their
worktree (e.g. ``/private/tmp/.../worktrees/worker_XXX/...``) where
these relative paths don't resolve. The worker responds "file does not
exist" and holds.

Fix: ``_prepare_initial_input`` must emit fully-qualified absolute
paths so the Read tool resolves them regardless of the worker's cwd.
Two parallel implementations exist — one in ``Supervisor`` and one in
``TmuxSessionService`` — and both must be fixed.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm-state/homes/claude_controller",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _long_prompt() -> str:
    # > 280 chars so _prepare_initial_input writes to disk and returns
    # the "Read <path>..." reference string instead of returning verbatim.
    return "x" * 500


# ---------------------------------------------------------------------------
# Supervisor._prepare_initial_input — absolute path emission
# ---------------------------------------------------------------------------


def test_supervisor_kickoff_contains_absolute_prompt_path(tmp_path: Path) -> None:
    """The kickoff string must reference the prompt file by absolute path."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    kickoff = supervisor._prepare_initial_input("operator", _long_prompt())

    expected_prompt = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )
    # The absolute prompt path must appear in the kickoff string.
    assert str(expected_prompt) in kickoff
    # And the fragile relative form must NOT appear — this is the bug
    # the fix is addressing (workers couldn't resolve it from their cwd).
    # Check that every occurrence of "control-prompts/operator.md" in the
    # kickoff is preceded by non-space characters (i.e., is part of an
    # absolute path), never at the start of a token.
    assert " control-prompts/operator.md" not in kickoff
    assert not kickoff.startswith("control-prompts/")
    assert kickoff.count(str(expected_prompt)) >= 1


def test_supervisor_kickoff_contains_absolute_system_path_when_present(
    tmp_path: Path,
) -> None:
    """When SYSTEM.md exists, the kickoff references it by absolute path."""
    config = _config(tmp_path)
    # Create the SYSTEM.md the real code will look for so the
    # instruct branch is exercised.
    instruct_path = tmp_path / ".pollypm" / "docs" / "SYSTEM.md"
    instruct_path.parent.mkdir(parents=True, exist_ok=True)
    instruct_path.write_text("# System reference\n")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    kickoff = supervisor._prepare_initial_input("operator", _long_prompt())

    # Absolute SYSTEM.md path is present.
    assert str(instruct_path) in kickoff
    # Relative form (what the bug emitted) is NOT present as a
    # free-standing reference — the absolute path starts with "/"
    # so ".pollypm/docs/SYSTEM.md " would only appear if we regressed.
    assert " .pollypm/docs/SYSTEM.md " not in kickoff


def test_supervisor_kickoff_paths_start_with_slash(tmp_path: Path) -> None:
    """Every path fragment in the kickoff must be absolute (start with '/')."""
    config = _config(tmp_path)
    instruct_path = tmp_path / ".pollypm" / "docs" / "SYSTEM.md"
    instruct_path.parent.mkdir(parents=True, exist_ok=True)
    instruct_path.write_text("# System reference\n")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    kickoff = supervisor._prepare_initial_input("operator", _long_prompt())

    # Locate each "Read <path>" token and verify the path is absolute.
    # The expected template is:
    #   "Read <absolute-sys> for system context, then read <absolute-prompt> for your role. ..."
    # We don't parse the exact wording — instead, we confirm both
    # known file references appear as absolute paths.
    prompt_path = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )
    assert str(prompt_path).startswith("/")
    assert str(instruct_path).startswith("/")
    assert str(prompt_path) in kickoff
    assert str(instruct_path) in kickoff


def test_supervisor_kickoff_prompt_file_actually_exists(tmp_path: Path) -> None:
    """The absolute path in the kickoff must point to an existing file.

    ``_prepare_initial_input`` writes the prompt file before returning;
    the absolute path it emits must therefore be a real file on disk.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    kickoff = supervisor._prepare_initial_input("operator", _long_prompt())

    prompt_path = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )
    assert prompt_path.exists()
    # And the kickoff references that very path.
    assert str(prompt_path) in kickoff


def test_supervisor_kickoff_resolves_from_foreign_cwd(
    monkeypatch, tmp_path: Path,
) -> None:
    """Simulate a worker whose cwd is outside the prompt directory.

    The whole point of using absolute paths is that the kickoff must
    work regardless of where the worker process is running from.
    Changing cwd to an unrelated directory must not affect path
    resolution for anything the kickoff string mentions.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    kickoff = supervisor._prepare_initial_input("operator", _long_prompt())
    prompt_path = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )

    # Pretend the worker is chdir'd into an unrelated worktree-like dir.
    foreign_cwd = tmp_path / "fake-worktree" / "deep" / "nested"
    foreign_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(foreign_cwd)

    # The absolute path in the kickoff resolves to the same real file
    # no matter the cwd.
    referenced = Path(str(prompt_path))
    assert referenced.is_absolute()
    assert referenced.exists()
    assert str(referenced) in kickoff


# ---------------------------------------------------------------------------
# TmuxSessionService._prepare_initial_input — parallel implementation
# ---------------------------------------------------------------------------


def test_session_service_kickoff_contains_absolute_prompt_path(
    tmp_path: Path,
) -> None:
    """Parallel check on the session_services/tmux.py code path."""
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)
    kickoff = service._prepare_initial_input(
        "operator",
        _long_prompt(),
        expected_window="pm-operator",
        session_role="operator-pm",
    )

    expected_prompt = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )
    assert str(expected_prompt) in kickoff
    # Fragile relative form (the bug) would appear as a bare token,
    # preceded by a space. The absolute form contains this string as a
    # suffix, so we check specifically for the space-prefixed bare form.
    assert " control-prompts/operator.md" not in kickoff


def test_session_service_kickoff_resolves_from_foreign_cwd(
    monkeypatch, tmp_path: Path,
) -> None:
    """Session-service path: absolute paths survive a foreign cwd too."""
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    instruct_path = tmp_path / ".pollypm" / "docs" / "SYSTEM.md"
    instruct_path.parent.mkdir(parents=True, exist_ok=True)
    instruct_path.write_text("# System reference\n")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)
    kickoff = service._prepare_initial_input(
        "operator",
        _long_prompt(),
        expected_window="pm-operator",
        session_role="operator-pm",
    )

    prompt_path = (
        tmp_path / ".pollypm-state" / "control-prompts" / "operator.md"
    )

    foreign_cwd = tmp_path / "fake-worktree"
    foreign_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(foreign_cwd)

    assert str(prompt_path) in kickoff
    assert str(instruct_path) in kickoff
    assert Path(str(prompt_path)).is_absolute()
    assert Path(str(instruct_path)).is_absolute()
    # Files referenced by the kickoff must all exist — otherwise the
    # worker still fails ("file does not exist").
    assert Path(str(prompt_path)).exists()
    assert Path(str(instruct_path)).exists()


def test_session_service_kickoff_worker_role_bypasses_check(
    tmp_path: Path,
) -> None:
    """Worker role skips the persona-swap assertion but still needs
    absolute paths in its kickoff (this is the E2E scenario from #263).
    """
    from pollypm.session_services.tmux import TmuxSessionService

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    service = TmuxSessionService(config=config, store=supervisor.store)
    kickoff = service._prepare_initial_input(
        "worker_e2e_auto_xxx",  # transient worker session name
        _long_prompt(),
        expected_window="worker_e2e_auto_xxx",
        session_role="worker",
    )

    expected_prompt = (
        tmp_path
        / ".pollypm-state"
        / "control-prompts"
        / "worker_e2e_auto_xxx.md"
    )
    assert str(expected_prompt) in kickoff
    assert Path(str(expected_prompt)).exists()
    # And the buggy relative form (a bare token preceded by a space)
    # must be absent — the absolute form starts with '/' not with
    # 'control-prompts/'.
    assert " control-prompts/worker_e2e_auto_xxx.md" not in kickoff
