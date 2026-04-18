"""Tests for the DefaultLaunchPlanner plugin.

These cover the Step 3 extraction: the planner produces the same
``SessionLaunchSpec`` output that the old ``Supervisor.plan_launches``
did, and the Supervisor's public methods correctly delegate to it.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.launch_planner import LaunchPlanner, get_launch_planner
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
from pollypm.plugins_builtin.default_launch_planner.planner import (
    DefaultLaunchPlanner,
    DefaultLaunchPlannerContext,
)
from pollypm.supervisor import Supervisor


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-heartbeat",
            ),
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


def _launch_signature(spec) -> tuple:
    """Stable tuple of the fields we want to compare across planners."""
    return (
        spec.session.name,
        spec.session.role,
        spec.session.provider.value,
        spec.session.account,
        spec.account.name,
        spec.window_name,
        str(spec.log_path),
        spec.command,
        str(spec.resume_marker) if spec.resume_marker else None,
        spec.initial_input,
        str(spec.fresh_launch_marker) if spec.fresh_launch_marker else None,
    )


def test_plugin_registers_launch_planner_kind(tmp_path: Path) -> None:
    """The built-in plugin exposes a launch planner under the kind."""
    config = _config(tmp_path)
    planner = get_launch_planner(
        "default",
        root_dir=config.project.root_dir,
        context=_planner_ctx_from_supervisor(Supervisor(config)),
    )
    assert isinstance(planner, DefaultLaunchPlanner)
    # Protocol conformance — duck-typed, but check the methods exist.
    assert hasattr(planner, "plan_launches")
    assert hasattr(planner, "effective_session")
    assert hasattr(planner, "tmux_session_for_launch")
    assert hasattr(planner, "launch_by_session")


def _planner_ctx_from_supervisor(sup: Supervisor) -> DefaultLaunchPlannerContext:
    """Construct a planner context from a Supervisor's helpers.

    Mirrors ``Supervisor._build_launch_planner`` so tests can exercise
    the planner directly with the same wiring.
    """
    return DefaultLaunchPlannerContext(
        config=sup.config,
        store=sup.store,
        readonly_state=sup.readonly_state,
        effective_account=sup._effective_account,
        apply_role_launch_restrictions=sup._apply_role_launch_restrictions,
        resolve_profile_prompt=sup._resolve_profile_prompt,
        storage_closet_session_name=sup.storage_closet_session_name,
    )


def test_default_planner_matches_supervisor_output(tmp_path: Path) -> None:
    """DefaultLaunchPlanner's plan matches what Supervisor returns (via delegation)."""
    config = _config(tmp_path)
    sup = Supervisor(config)
    sup.ensure_layout()

    # Drive the Supervisor delegator — this IS the planner today.
    via_supervisor = [_launch_signature(l) for l in sup.plan_launches()]

    # Build a fresh standalone planner instance and compare.
    standalone = DefaultLaunchPlanner(_planner_ctx_from_supervisor(sup))
    via_standalone = [_launch_signature(l) for l in standalone.plan_launches()]

    assert via_standalone == via_supervisor
    # And the plan is non-trivial so the comparison is meaningful.
    assert len(via_supervisor) == 2
    names = {spec[0] for spec in via_supervisor}
    assert names == {"heartbeat", "operator"}


def test_default_planner_controller_override_matches(tmp_path: Path) -> None:
    """Override path also matches between standalone planner and Supervisor."""
    config = _config(tmp_path)
    # Add a backup account so the override path has somewhere to go.
    config.accounts["codex_backup"] = AccountConfig(
        name="codex_backup",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        home=tmp_path / ".pollypm/homes/codex_backup",
    )
    sup = Supervisor(config)
    sup.ensure_layout()

    # Both paths should produce identical plans under the override.
    via_supervisor = [
        _launch_signature(l)
        for l in sup.plan_launches(controller_account="codex_backup")
    ]
    standalone = DefaultLaunchPlanner(_planner_ctx_from_supervisor(sup))
    via_standalone = [
        _launch_signature(l)
        for l in standalone.plan_launches(controller_account="codex_backup")
    ]
    assert via_standalone == via_supervisor


def test_supervisor_launch_planner_property_returns_default(tmp_path: Path) -> None:
    """Supervisor exposes the configured planner via the ``launch_planner`` property."""
    config = _config(tmp_path)
    sup = Supervisor(config)
    planner = sup.launch_planner
    assert isinstance(planner, DefaultLaunchPlanner)
    # The property caches — same instance on subsequent access.
    assert sup.launch_planner is planner


def test_invalidate_launch_cache_delegates(tmp_path: Path) -> None:
    """Supervisor.invalidate_launch_cache clears the planner's cache."""
    config = _config(tmp_path)
    sup = Supervisor(config)
    sup.ensure_layout()

    first = sup.plan_launches()
    # Cached path — same list instance on second call (no override).
    assert sup.plan_launches() is first
    sup.invalidate_launch_cache()
    refreshed = sup.plan_launches()
    # Recomputed — new list instance, equal contents.
    assert refreshed is not first
    assert [l.session.name for l in refreshed] == [l.session.name for l in first]


def test_tmux_session_for_launch_delegates(tmp_path: Path) -> None:
    """Supervisor.tmux_session_for_launch delegates to the planner."""
    config = _config(tmp_path)
    sup = Supervisor(config)
    sup.ensure_layout()
    launch = next(l for l in sup.plan_launches() if l.session.name == "operator")
    # Planner and Supervisor should agree on placement.
    assert sup.tmux_session_for_launch(launch) == sup.launch_planner.tmux_session_for_launch(launch)
    # And it's the storage-closet session today.
    assert sup.tmux_session_for_launch(launch) == sup.storage_closet_session_name()


def test_launch_by_session_unknown_raises(tmp_path: Path) -> None:
    """Unknown session names raise KeyError through delegation."""
    config = _config(tmp_path)
    sup = Supervisor(config)
    sup.ensure_layout()
    try:
        sup.launch_by_session("nonexistent")
    except KeyError as exc:
        assert "nonexistent" in str(exc)
    else:
        raise AssertionError("Expected KeyError for unknown session")
