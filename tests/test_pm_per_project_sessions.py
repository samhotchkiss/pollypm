"""Per-project PM session synthesis.

The launch planner auto-injects a synthetic ``pm_<project>``
``operator-pm`` SessionConfig for every tracked project that doesn't
already carry an explicit project-scoped operator-pm session in the
static config. The synthesis runs inside ``plan_launches`` so existing
installs pick up per-project PMs on the next ``pm up`` without an
explicit config migration — Sam's deployed ``pollypm.toml`` stays
untouched (per the per-project PM rollout migration plan).

These tests pin:

* Three tracked projects → three ``pm_<project>`` synthetic entries.
* The workspace project (``pollypm``) is NOT auto-synthesized — its
  workspace-level ``operator`` session already serves that lane.
* Untracked projects opt out of synthesis (matches reviewer
  auto-provisioning's tracked-only contract).
* Explicit ``operator-pm`` session entries in the static config WIN
  — the synthesizer leaves them alone (no double-injection).
* No compatible account → the project is silently skipped (project
  is left without a per-project PM, doesn't crash the planner).
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
from pollypm.plugins_builtin.default_launch_planner.planner import (
    DefaultLaunchPlannerContext,
)
from pollypm.supervisor import Supervisor


def _planner_ctx_from_supervisor(sup: Supervisor) -> DefaultLaunchPlannerContext:
    return DefaultLaunchPlannerContext(
        config=sup.config,
        store=sup.store,
        readonly_state=sup.readonly_state,
        effective_account=sup._effective_account,
        apply_role_launch_restrictions=sup._apply_role_launch_restrictions,
        resolve_profile_prompt=sup._resolve_profile_prompt,
        storage_closet_session_name=sup.storage_closet_session_name,
    )


def _base_config(
    tmp_path: Path, *, tracked_projects: dict[str, Path] | None = None,
) -> PollyPMConfig:
    """Shape mirrors tests/test_launch_planner.py::_config, with a few extra
    tracked projects so the synthesis path has something to chew on.

    Every project listed in ``tracked_projects`` is registered with
    ``tracked=True``; the workspace ``pollypm`` project is registered
    unconditionally (matches every real install).
    """
    projects: dict[str, KnownProject] = {
        "pollypm": KnownProject(
            key="pollypm",
            path=tmp_path,
            name="PollyPM",
            kind=ProjectKind.FOLDER,
            tracked=True,
        ),
    }
    for key, path in (tracked_projects or {}).items():
        path.mkdir(parents=True, exist_ok=True)
        projects[key] = KnownProject(
            key=key,
            path=path,
            name=key,
            kind=ProjectKind.FOLDER,
            tracked=True,
            persona_name=f"PMof{key}",
        )
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
        projects=projects,
    )


def test_three_tracked_projects_emit_three_pm_sessions(tmp_path: Path) -> None:
    """Per the spec: 3 tracked projects → 3 ``pm_<project>`` entries."""
    config = _base_config(
        tmp_path,
        tracked_projects={
            "samblog": tmp_path / "samblog",
            "savethenovel": tmp_path / "savethenovel",
            "blackjack-trainer": tmp_path / "blackjack-trainer",
        },
    )
    sup = Supervisor(config)
    sup.ensure_layout()

    launches = sup.plan_launches()
    pm_launches = [
        spec for spec in launches
        if spec.session.role == "operator-pm"
        and spec.session.name != "operator"
    ]
    pm_names = sorted(spec.session.name for spec in pm_launches)
    assert pm_names == ["pm_blackjack-trainer", "pm_samblog", "pm_savethenovel"]

    # Each synthetic PM is scoped to its project, lands in
    # ``pm-<project>`` (the canonical per-project PM window), and
    # routes through Claude (the dual-provider role-routing default
    # for ``operator-pm`` is ``opus-4.7``).
    samblog = next(spec for spec in pm_launches if spec.session.name == "pm_samblog")
    assert samblog.session.project == "samblog"
    assert samblog.window_name == "pm-samblog"
    assert samblog.session.provider is ProviderKind.CLAUDE
    # CWD points at the project's actual on-disk path (not a worktree)
    # because the PM observes / coordinates; it does not write code.
    assert samblog.session.cwd == tmp_path / "samblog"
    # Profile is ``polly`` so the agent gets the canonical operator
    # prompt; the project persona (Archie / Sage / etc.) is injected
    # at jump-to-PM time via the cockpit's PM-session re-anchoring
    # primer (cockpit_rail._build_project_pm_primer).
    assert samblog.session.agent_profile == "polly"


def test_workspace_pollypm_project_is_not_auto_synthesized(tmp_path: Path) -> None:
    """The workspace project is served by the workspace ``operator`` session.

    Auto-injecting a duplicate would create two rail rows pointing at
    the same conceptual surface (PM Chat for pollypm vs the workspace
    Polly row). The synthesizer skips ``project_key == "pollypm"``.
    """
    config = _base_config(
        tmp_path,
        tracked_projects={"samblog": tmp_path / "samblog"},
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    pm_names = sorted(
        spec.session.name for spec in sup.plan_launches()
        if spec.session.role == "operator-pm" and spec.session.name != "operator"
    )
    assert pm_names == ["pm_samblog"]
    assert "pm_pollypm" not in pm_names


def test_untracked_projects_opt_out_of_synthesis(tmp_path: Path) -> None:
    """Untracked projects don't get a per-project PM session.

    Mirrors the reviewer auto-provisioning contract — untracked
    projects opt out of every auto-provision sweep (see
    recovery.reviewer_provisioning and project_v1_tag memory).
    """
    config = _base_config(tmp_path)
    # Add a project that is NOT tracked.
    untracked_path = tmp_path / "untracked"
    untracked_path.mkdir()
    config.projects["untracked"] = KnownProject(
        key="untracked",
        path=untracked_path,
        name="untracked",
        kind=ProjectKind.FOLDER,
        tracked=False,
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    pm_names = sorted(
        spec.session.name for spec in sup.plan_launches()
        if spec.session.role == "operator-pm" and spec.session.name != "operator"
    )
    assert pm_names == []  # only the workspace operator, no synthesized PMs


def test_explicit_pm_session_in_config_wins_over_synthesis(tmp_path: Path) -> None:
    """A user-declared project-scoped operator-pm session is authoritative.

    The static-config entry is left alone — no double-injection.
    """
    config = _base_config(
        tmp_path,
        tracked_projects={"samblog": tmp_path / "samblog"},
    )
    config.sessions["pm_samblog"] = SessionConfig(
        name="pm_samblog",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_controller",
        cwd=tmp_path / "samblog",
        project="samblog",
        window_name="pm-samblog",
        agent_profile="polly",
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    pm_launches = [
        spec for spec in sup.plan_launches()
        if spec.session.role == "operator-pm" and spec.session.name != "operator"
    ]
    # Exactly one ``pm_samblog`` — the explicit static-config entry,
    # not a duplicate from synthesis.
    assert [spec.session.name for spec in pm_launches] == ["pm_samblog"]


def test_planner_skips_pm_synthesis_when_no_compatible_account(tmp_path: Path) -> None:
    """No compatible account → project is silently skipped, no crash.

    Mirrors the existing routed-role behavior in ``effective_session``
    (no compatible account → warn-and-skip). The launch plan still
    produces something useful for the rest of the workspace.
    """
    config = _base_config(
        tmp_path,
        tracked_projects={"samblog": tmp_path / "samblog"},
    )
    # Pin samblog's operator-pm role to a Codex alias, but configure
    # zero Codex accounts → no compatible account, project skipped.
    from pollypm.models import ModelAssignment

    config.projects["samblog"].role_assignments["operator_pm"] = ModelAssignment(
        alias="codex-gpt-5.4",
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    pm_launches = [
        spec for spec in sup.plan_launches()
        if spec.session.role == "operator-pm" and spec.session.name != "operator"
    ]
    # No ``pm_samblog`` because Codex routing has no compatible
    # account; the rest of the plan is unaffected.
    assert pm_launches == []
    # The plan itself is still produced (heartbeat + workspace operator).
    other_names = sorted(spec.session.name for spec in sup.plan_launches())
    assert "heartbeat" in other_names
    assert "operator" in other_names


def test_synthesizer_uses_per_project_role_override(tmp_path: Path) -> None:
    """Per-project ``role_assignments`` for ``operator_pm`` flow through.

    The default for dual-Claude+Codex setups is Opus on Claude; pin
    samblog to Codex via a per-project override (with a Codex account
    available) and confirm the synthesized session lands on Codex.
    """
    from pollypm.models import ModelAssignment

    config = _base_config(
        tmp_path,
        tracked_projects={"samblog": tmp_path / "samblog"},
    )
    # Add a Codex account so the override can resolve.
    config.accounts["codex_backup"] = AccountConfig(
        name="codex_backup",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        home=tmp_path / ".pollypm/homes/codex_backup",
    )
    config.projects["samblog"].role_assignments["operator_pm"] = ModelAssignment(
        alias="codex-gpt-5.4",
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    samblog = next(
        spec for spec in sup.plan_launches() if spec.session.name == "pm_samblog"
    )
    assert samblog.session.provider is ProviderKind.CODEX
    assert samblog.session.account == "codex_backup"


def test_rail_project_session_map_prefers_per_project_pm_over_worker() -> None:
    """``CockpitRouter._project_session_map`` routes Chat PM to the
    per-project PM session, not the worker / architect.

    Pre-per-project-pm-rollout: ``_project_session_map`` skipped all
    control roles (including ``operator-pm``) and picked the first
    project session — typically a worker. After the rollout, the
    synthetic ``pm_<project>`` operator-pm wins for project chat.

    The workspace ``operator`` session (workspace Polly) is excluded
    from the map even though its ``session.project`` is ``"pollypm"``
    — the workspace operator owns the workspace rail row, not the
    per-project Chat PM lane.
    """
    from pollypm.cockpit_rail import CockpitRouter

    router = CockpitRouter.__new__(CockpitRouter)

    class _Launch:
        def __init__(self, name: str, role: str, project: str) -> None:
            self.session = type(
                "_S",
                (),
                {"name": name, "role": role, "project": project},
            )()

    launches = [
        _Launch("operator", "operator-pm", "pollypm"),
        _Launch("worker_samblog", "worker", "samblog"),
        _Launch("pm_samblog", "operator-pm", "samblog"),
        _Launch("architect_savethenovel", "architect", "savethenovel"),
        # Bikepath has only a worker (no per-project PM provisioned).
        _Launch("worker_bikepath", "worker", "bikepath"),
    ]
    mapping = router._project_session_map(launches)
    # Per-project PM wins for samblog.
    assert mapping["samblog"] == "pm_samblog"
    # No per-project PM → fall back to the legacy worker/architect pick.
    assert mapping["savethenovel"] == "architect_savethenovel"
    assert mapping["bikepath"] == "worker_bikepath"
    # Workspace operator does NOT appear under the workspace project.
    assert "pollypm" not in mapping


def test_synthesis_is_deterministic_across_calls(tmp_path: Path) -> None:
    """plan_launches() caches; repeated calls return identical plans.

    Regression guard: synthesis is purely a function of config; the
    cached path returns the same instance and an invalidated path
    returns an equal plan.
    """
    config = _base_config(
        tmp_path,
        tracked_projects={
            "samblog": tmp_path / "samblog",
            "savethenovel": tmp_path / "savethenovel",
        },
    )
    sup = Supervisor(config)
    sup.ensure_layout()
    first = sup.plan_launches()
    assert sup.plan_launches() is first  # cached
    sup.invalidate_launch_cache()
    refreshed = sup.plan_launches()
    assert refreshed is not first
    assert [spec.session.name for spec in refreshed] == [
        spec.session.name for spec in first
    ]
