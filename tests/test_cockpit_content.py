import subprocess
import sys

from pollypm.cockpit_content import (
    CockpitContentContext,
    ErrorPane,
    FallbackPane,
    LiveAgentPane,
    LoadingPane,
    TextualCommandPane,
    loading_content_plan,
    resolve_cockpit_content,
)


def _ctx() -> CockpitContentContext:
    return CockpitContentContext.from_projects(
        ["demo", "other"],
        project_sessions={
            "demo": "worker_demo",
            "other": "worker_other",
        },
    )


def test_importing_content_resolver_does_not_load_ui_or_supervisor() -> None:
    code = "\n".join(
        [
            "import sys",
            "import pollypm.cockpit_content",
            "bad = [name for name in (",
            "    'pollypm.cockpit_ui',",
            "    'pollypm.cockpit_rail',",
            "    'pollypm.service_api',",
            "    'pollypm.supervisor',",
            ") if name in sys.modules]",
            "assert not bad, bad",
        ]
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_loading_content_plan_is_typed_placeholder() -> None:
    plan = loading_content_plan("project:demo", label="Demo")

    assert isinstance(plan, LoadingPane)
    assert plan.content_kind == "loading"
    assert plan.right_pane_state == "loading"
    assert plan.selected_key == "project:demo"
    assert plan.message == "Loading Demo..."


def test_resolves_polly_and_russell_to_live_agent_panes() -> None:
    polly = resolve_cockpit_content("polly", _ctx())
    russell = resolve_cockpit_content("russell", _ctx())

    assert isinstance(polly, LiveAgentPane)
    assert polly.session_name == "operator"
    assert polly.fallback is not None
    assert polly.fallback.pane_kind == "polly"

    assert isinstance(russell, LiveAgentPane)
    assert russell.session_name == "reviewer"


def test_resolves_global_static_textual_command_panes() -> None:
    expected = {
        "dashboard": ("dashboard", ("cockpit-pane", "dashboard")),
        "inbox": ("inbox", ("cockpit-pane", "inbox")),
        "settings": ("settings", ("cockpit-pane", "settings")),
        "metrics": ("metrics", ("cockpit-pane", "metrics")),
        "activity": ("activity", ("cockpit-pane", "activity")),
    }

    for route_key, (pane_kind, command_args) in expected.items():
        plan = resolve_cockpit_content(route_key, _ctx())
        assert isinstance(plan, TextualCommandPane)
        assert plan.content_kind == "static_command"
        assert plan.pane_kind == pane_kind
        assert plan.command_args == command_args
        assert plan.selected_key == route_key


def test_resolves_scoped_inbox_and_activity_to_project_filtered_commands() -> None:
    inbox = resolve_cockpit_content("inbox:demo", _ctx())
    activity = resolve_cockpit_content("activity:demo", _ctx())

    assert isinstance(inbox, TextualCommandPane)
    assert inbox.pane_kind == "inbox"
    assert inbox.project_key == "demo"
    assert inbox.selected_key == "inbox"
    assert inbox.command_args == ("cockpit-pane", "inbox", "--project", "demo")

    assert isinstance(activity, TextualCommandPane)
    assert activity.pane_kind == "activity"
    assert activity.project_key == "demo"
    assert activity.selected_key == "activity:demo"
    assert activity.command_args == ("cockpit-pane", "activity", "--project", "demo")


def test_resolves_project_dashboard_settings_issues_and_task_panes() -> None:
    cases = {
        "project:demo": (
            "project",
            "project:demo:dashboard",
            ("cockpit-pane", "project", "demo"),
            None,
        ),
        "project:demo:dashboard": (
            "project",
            "project:demo:dashboard",
            ("cockpit-pane", "project", "demo"),
            None,
        ),
        "project:demo:settings": (
            "settings",
            "project:demo:settings",
            ("cockpit-pane", "settings", "demo"),
            None,
        ),
        "project:demo:issues": (
            "issues",
            "project:demo:issues",
            ("cockpit-pane", "issues", "demo"),
            None,
        ),
        "project:demo:issues:task:7": (
            "issues",
            "project:demo:issues:task:7",
            ("cockpit-pane", "issues", "demo", "--task", "demo/7"),
            "demo/7",
        ),
        "project:demo:task:7": (
            "issues",
            "project:demo:task:7",
            ("cockpit-pane", "issues", "demo", "--task", "demo/7"),
            "demo/7",
        ),
    }

    for route_key, (pane_kind, selected_key, command_args, task_id) in cases.items():
        plan = resolve_cockpit_content(route_key, _ctx())
        assert isinstance(plan, TextualCommandPane)
        assert plan.project_key == "demo"
        assert plan.pane_kind == pane_kind
        assert plan.selected_key == selected_key
        assert plan.command_args == command_args
        assert plan.task_id == task_id


def test_resolves_project_pm_chat_to_exact_project_session_mapping() -> None:
    plan = resolve_cockpit_content("project:demo:session", _ctx())

    assert isinstance(plan, LiveAgentPane)
    assert plan.project_key == "demo"
    assert plan.session_name == "worker_demo"
    assert plan.fallback is not None
    assert plan.fallback.pane_kind == "project"
    assert plan.fallback.command_args == ("cockpit-pane", "project", "demo")


def test_missing_worker_returns_explicit_fallback_not_another_worker() -> None:
    context = CockpitContentContext.from_projects(
        ["demo", "other"],
        project_sessions={"other": "worker_other"},
    )

    plan = resolve_cockpit_content("project:demo:session", context)

    assert isinstance(plan, FallbackPane)
    assert plan.reason == "missing_worker"
    assert not hasattr(plan, "session_name")
    assert "worker_other" not in repr(plan)
    assert plan.fallback.pane_kind == "project"
    assert plan.fallback.project_key == "demo"
    assert plan.fallback.command_args == ("cockpit-pane", "project", "demo")


def test_missing_project_returns_error_content() -> None:
    context = CockpitContentContext.from_projects(["demo"])

    plan = resolve_cockpit_content("project:missing:dashboard", context)

    assert isinstance(plan, ErrorPane)
    assert plan.reason == "missing_project"
    assert "missing" in plan.message


def test_invalid_route_returns_error_content() -> None:
    plan = resolve_cockpit_content("not-a-route", _ctx())

    assert isinstance(plan, ErrorPane)
    assert plan.reason == "unknown_route"
    assert plan.right_pane_state == "error"
