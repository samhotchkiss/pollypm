from __future__ import annotations

import base64
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import typer

from pollypm.plugins_builtin.core_agent_profiles.profiles import heartbeat_prompt, polly_prompt
from pollypm.config import DEFAULT_CONFIG_PATH, load_config, write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.projects import (
    DEFAULT_WORKSPACE_ROOT,
    commit_initial_scaffold,
    discover_recent_git_repositories,
    ensure_project_scaffold,
    make_project_key,
)
from pollypm.runtime_env import provider_profile_env_for_provider
from typing import TYPE_CHECKING

from pollypm.onboarding_models import (  # noqa: F401  (re-exported)
    CliAvailability,
    ConnectedAccount,
    LoginPreferences,
    OnboardingResult,
    ProviderChoice,
)
from pollypm.onboarding_ui import (  # noqa: F401  (re-exported)
    available_clis as _available_clis,
    provider_choices as _provider_choices,
    render_account_step_intro as _render_account_step_intro,
    render_connected_account as _render_connected_account,
    render_intro as _render_intro,
    render_provider_choices as _render_provider_choices,
)
from pollypm.session_services import create_tmux_client

if TYPE_CHECKING:
    from pollypm.tmux.client import TmuxClient


DEMO_PROJECT_BASENAME = "demo-polly"
DEMO_PROJECT_MARKER = ".pollypm-demo-fallback"
DEMO_PROJECT_TEMPLATE_DIR = Path(__file__).resolve().parent / "defaults" / "demo"
DEMO_PROJECT_TASK_TITLE = "Fix the demo queue estimate bug"
DEMO_PROJECT_TASK_DESCRIPTION = (
    "The offline demo intentionally ships a tiny regression in demo_app.py.\n"
    "Open TASK.md and tests/test_demo_app.py, fix estimate_focus_minutes(), "
    "and keep the seeded PollyPM task until you are done."
)
DEMO_PROJECT_REPLAY_COMMIT_COUNT = 3
DEMO_PROJECT_REPLAY_HISTORY_FILES = {
    "demo_history.md",
}
DEMO_PROJECT_REPLAY_TASK_FILES = {
    "TASK.md",
    "tests/test_demo_history.py",
    "tests/test_demo_task.py",
}


class LoginCancelled(Exception):
    pass


def default_session_args(
    provider: ProviderKind,
    *,
    open_permissions: bool = True,
    role: str = "",
    model: str | None = None,
) -> list[str]:
    """Dispatch the role-to-CLI-flag mapping to the provider package.

    Each provider owns its own flag vocabulary (Claude uses
    ``--allowedTools``; Codex uses ``--sandbox``) — see
    :mod:`pollypm.providers.claude.session_args` and
    :mod:`pollypm.providers.codex.session_args`. Onboarding stays out
    of the per-flag details so a third-party provider can ship its
    own ``session_args`` without patching this module.
    """
    if provider is ProviderKind.CLAUDE:
        from pollypm.providers.claude.session_args import session_args

        return session_args(
            open_permissions=open_permissions,
            role=role,
            model=model,
        )
    if provider is ProviderKind.CODEX:
        from pollypm.providers.codex.session_args import session_args

        return session_args(
            open_permissions=open_permissions,
            role=role,
            model=model,
        )
    return []


def default_control_args(
    provider: ProviderKind,
    *,
    open_permissions: bool = True,
    role: str = "",
    model: str | None = None,
) -> list[str]:
    return default_session_args(
        provider,
        open_permissions=open_permissions,
        role=role,
        model=model,
    )


def _prime_claude_home(home: Path) -> None:
    """Back-compat shim — real impl lives in the Claude provider package.

    Tests and a handful of legacy callers (``supervisor``, ``accounts``,
    ``supervision.control_home``) still import this name; #406 moved
    the body into :func:`pollypm.providers.claude.onboarding.prime_claude_home`
    and kept this dispatcher so monkeypatching
    ``pollypm.onboarding._prime_claude_home`` still works.
    """
    from pollypm.providers.claude.onboarding import prime_claude_home

    prime_claude_home(home)


def _detect_host_claude_login() -> tuple[bool, str | None]:
    home = Path.home()
    credentials_path = home / ".claude" / ".credentials.json"
    if not credentials_path.exists():
        return (False, None)
    try:
        from pollypm.providers.claude.detect import detect_claude_email

        email = detect_claude_email(home)
    except Exception:  # noqa: BLE001
        return (False, None)
    return (bool(email), email)


def _detect_host_codex_login() -> tuple[bool, str | None]:
    home = Path.home()
    auth_path = home / ".codex" / "auth.json"
    if not auth_path.exists():
        return (False, None)
    try:
        from pollypm.providers.codex.detect import detect_codex_email

        email = detect_codex_email(home)
    except Exception:  # noqa: BLE001
        return (False, None)
    return (bool(email), email)


def _smoke_test_host_login(provider: ProviderKind) -> bool:
    try:
        if provider is ProviderKind.CLAUDE:
            result = subprocess.run(
                ["claude", "auth", "status", "--json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return False
            try:
                data = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                return False
            return bool(data.get("loggedIn"))
        result = subprocess.run(
            ["codex", "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return False
        text = (result.stdout + result.stderr).lower()
        return "not logged in" not in text and "sign in" not in text
    except Exception:  # noqa: BLE001
        return False


def _detected_host_account(provider: ProviderKind) -> ConnectedAccount | None:
    if provider is ProviderKind.CLAUDE:
        logged_in, email = _detect_host_claude_login()
    elif provider is ProviderKind.CODEX:
        logged_in, email = _detect_host_codex_login()
    else:
        return None
    if not logged_in or not email:
        return None
    if not _smoke_test_host_login(provider):
        return None
    return ConnectedAccount(
        provider=provider,
        email=email,
        account_name=_slugify_email(provider, email),
        home=None,
    )


def _select_provider_to_connect(installed: list[CliAvailability], accounts: dict[str, ConnectedAccount]) -> ProviderKind | None:
    if len(installed) == 1:
        if any(account.provider is installed[0].provider for account in accounts.values()):
            return None
        if not accounts:
            _render_account_step_intro(installed, accounts)
            typer.prompt("Press Return to start", default="", show_default=False)
        return installed[0].provider

    _render_account_step_intro(installed, accounts)
    choices = _provider_choices(installed, accounts)
    _render_provider_choices(choices)
    choice = typer.prompt("Choose", default="1")
    for item in choices:
        if choice == item.key:
            return item.provider
    return None


def _next_account_index(accounts: dict[str, ConnectedAccount], provider: ProviderKind) -> int:
    return len([account for account in accounts.values() if account.provider is provider]) + 1


def _connect_accounts_interactively(
    tmux: TmuxClient,
    *,
    root_dir: Path,
    accounts: dict[str, ConnectedAccount],
    available: list[CliAvailability],
) -> dict[str, ConnectedAccount]:
    installed = [item for item in available if item.installed]
    while True:
        provider = _select_provider_to_connect(installed, accounts)
        if provider is None:
            break
        account = _connect_account_via_tmux(
            tmux,
            root_dir=root_dir,
            provider=provider,
            index=_next_account_index(accounts, provider),
        )
        if account.account_name in accounts:
            raise typer.BadParameter(
                f"Duplicate connected account detected for {account.email}. "
                "Each connected account email must be unique."
            )
        accounts[account.account_name] = account
        typer.echo("")
        _render_connected_account(account, len(accounts))
        typer.echo("")
        if not typer.confirm("Connect another account?", default=True):
            break
    return accounts


def _scan_recent_projects(config_path: Path) -> list[KnownProject]:
    discovered = discover_recent_project_candidates(config_path)
    if not discovered:
        typer.echo("No recently active git repos were found in your home folder.")
        demo_path = demo_project_fallback_destination(config_path)
        typer.echo(
            f"You can copy a self-contained demo repo to {demo_path} and use it offline."
        )
        if typer.confirm("Copy the demo repo and use it as your first project?", default=True):
            return add_selected_projects(
                config_path,
                [provision_demo_project_fallback(config_path)],
            )
        return []

    typer.echo("")
    typer.echo("PollyPM found recently active repositories:")
    selected: list[Path] = []
    for repo_path in discovered:
        if typer.confirm(f"Add project {repo_path.name} at {repo_path}?", default=True):
            selected.append(repo_path)
    return add_selected_projects(config_path, selected)


def discover_recent_project_candidates(
    config_path: Path,
    *,
    scan_root: Path | None = None,
    recent_days: int = 14,
) -> list[Path]:
    try:
        config = load_config(config_path)
        known_paths = {project.path.resolve() for project in config.projects.values()}
    except Exception:  # noqa: BLE001
        known_paths = set()
    return discover_recent_git_repositories(
        scan_root or Path.home(),
        known_paths=known_paths,
        recent_days=recent_days,
    )


def _workspace_root_for_onboarding(config_path: Path) -> Path:
    try:
        config = load_config(config_path)
        workspace_root = getattr(config.project, "workspace_root", DEFAULT_WORKSPACE_ROOT)
    except Exception:  # noqa: BLE001
        workspace_root = DEFAULT_WORKSPACE_ROOT
    return Path(workspace_root).expanduser()


def demo_project_fallback_destination(config_path: Path) -> Path:
    del config_path
    workspace_root = Path.home()
    for index in range(100):
        suffix = "" if index == 0 else f"-{index}"
        candidate = workspace_root / f"{DEMO_PROJECT_BASENAME}{suffix}"
        if (candidate / DEMO_PROJECT_MARKER).exists():
            return candidate
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not find a free path for the onboarding demo repo.")


def _write_demo_project_files(target: Path) -> None:
    marker_path = target / DEMO_PROJECT_MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("demo-fallback-v1\n", encoding="utf-8")
    for source in sorted(DEMO_PROJECT_TEMPLATE_DIR.rglob("*")):
        if not source.is_file():
            continue
        relative_name = source.relative_to(DEMO_PROJECT_TEMPLATE_DIR)
        destination = target / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _demo_repo_stage_one_contents() -> dict[str, str]:
    return {
        "README.md": (
            "# PollyPM Demo Repo\n\n"
            "This offline fallback gives onboarding a tiny local project to copy.\n\n"
            "It ships a queue-summary helper, a small CLI, sample data, and tests.\n"
        ),
        "demo_app.py": (
            '"""Small offline demo for PollyPM onboarding."""\n\n'
            "from __future__ import annotations\n\n\n"
            "def summarize_queue(items: list[str]) -> str:\n"
            "    cleaned = [item.strip() for item in items if item.strip()]\n"
            "    if not cleaned:\n"
            '        return "No tasks queued."\n'
            "    if len(cleaned) == 1:\n"
            '        return f"1 task queued: {cleaned[0]}"\n'
            '    preview = ", ".join(cleaned[:2])\n'
            "    if len(cleaned) > 2:\n"
            '        preview += f", +{len(cleaned) - 2} more"\n'
            '    return f"{len(cleaned)} tasks queued: {preview}"\n\n\n'
            "def estimate_focus_minutes(task_count: int, *, per_task: int = 25) -> int:\n"
            "    if task_count <= 0:\n"
            "        return 0\n"
            "    if task_count == 1:\n"
            "        return 30\n"
            "    return task_count * per_task\n"
        ),
        "demo_cli.py": (
            "from __future__ import annotations\n\n"
            "import argparse\n\n"
            "from demo_app import estimate_focus_minutes, summarize_queue\n"
            "from demo_data import demo_task_titles\n\n\n"
            "def build_parser() -> argparse.ArgumentParser:\n"
            '    parser = argparse.ArgumentParser(prog="demo-polly")\n'
            "    subparsers = parser.add_subparsers(dest=\"command\", required=True)\n"
            '    subparsers.add_parser("summary", help="Print the queue summary.")\n'
            '    subparsers.add_parser("tasks", help="Print the sample demo tasks.")\n'
            '    subparsers.add_parser("focus", help="Print the focus estimate.")\n'
            "    return parser\n\n\n"
            "def main(argv: list[str] | None = None) -> int:\n"
            "    args = build_parser().parse_args(argv)\n"
            "    if args.command == \"summary\":\n"
            "        print(summarize_queue(demo_task_titles()))\n"
            "        return 0\n"
            "    if args.command == \"tasks\":\n"
            "        for title in demo_task_titles():\n"
            "            print(f\"- {title}\")\n"
            "        return 0\n"
            "    if args.command == \"focus\":\n"
            "        print(f\"{estimate_focus_minutes(len(demo_task_titles()))} minutes\")\n"
            "        return 0\n"
            "    return 1\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    raise SystemExit(main())\n"
        ),
        "demo_data.py": (
            '"""Sample tasks for the offline PollyPM demo."""\n\n'
            "from __future__ import annotations\n\n"
            "DEMO_TASKS: list[dict[str, str]] = [\n"
            '    {"title": "Fix the queue estimate bug", "kind": "bug"},\n'
            '    {"title": "Write a demo launch note", "kind": "docs"},\n'
            '    {"title": "Review the seeded git history", "kind": "ops"},\n'
            "]\n\n\n"
            "def demo_task_titles() -> list[str]:\n"
            '    return [item["title"] for item in DEMO_TASKS]\n'
        ),
        "demo_history.md": (
            "# Demo History\n\n"
            "This repo is replayable: onboarding seeds a short git history so the\n"
            "demo feels like a real project instead of a single snapshot.\n"
        ),
        ".gitignore": ".pollypm/\n__pycache__/\n.pytest_cache/\n",
        "tests/test_demo_app.py": (
            "import unittest\n\n"
            "from demo_app import estimate_focus_minutes, summarize_queue\n\n\n"
            "class DemoAppTests(unittest.TestCase):\n"
            "    def test_summarize_queue_handles_empty_input(self) -> None:\n"
            '        self.assertEqual(summarize_queue([]), "No tasks queued.")\n\n'
            "    def test_summarize_queue_shows_preview_and_count(self) -> None:\n"
            '        summary = summarize_queue(["plan onboarding", "write docs", "ship fallback"])\n'
            '        self.assertEqual(summary, "3 tasks queued: plan onboarding, write docs, +1 more")\n\n'
            "    def test_estimate_focus_minutes_uses_default_block(self) -> None:\n"
            "        self.assertEqual(estimate_focus_minutes(3), 75)\n\n"
            "    def test_estimate_focus_minutes_reserves_a_half_hour_for_one_task(self) -> None:\n"
            "        self.assertEqual(estimate_focus_minutes(1), 30)\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    unittest.main()\n"
        ),
        "tests/test_demo_cli.py": (
            "from contextlib import redirect_stdout\n"
            "from io import StringIO\n\n"
            "from demo_cli import main\n\n\n"
            "def test_summary_command_prints_queue_summary() -> None:\n"
            "    buffer = StringIO()\n"
            "    with redirect_stdout(buffer):\n"
            '        assert main(["summary"]) == 0\n'
            "    output = buffer.getvalue()\n"
            '    assert "tasks queued" in output\n'
            '    assert "Fix the queue estimate bug" in output\n'
        ),
        "tests/test_demo_data.py": (
            "from demo_data import DEMO_TASKS, demo_task_titles\n\n\n"
            "def test_demo_data_exposes_three_sample_tasks() -> None:\n"
            "    assert len(DEMO_TASKS) == 3\n"
            '    assert demo_task_titles()[0] == "Fix the queue estimate bug"\n'
        ),
    }


def _demo_repo_stage_two_contents() -> dict[str, str]:
    stage = _demo_repo_stage_one_contents()
    stage["README.md"] = (
        "# PollyPM Demo Repo\n\n"
        "The offline demo intentionally carries a small queue-estimate regression.\n"
        "Run the unit tests, fix the bug in `demo_app.py`, and keep the seeded task\n"
        "while you work.\n"
    )
    stage["demo_app.py"] = (
        '"""Small offline demo for PollyPM onboarding."""\n\n'
        "from __future__ import annotations\n\n\n"
        "def summarize_queue(items: list[str]) -> str:\n"
        "    cleaned = [item.strip() for item in items if item.strip()]\n"
        "    if not cleaned:\n"
        '        return "No tasks queued."\n'
        "    if len(cleaned) == 1:\n"
        '        return f"1 task queued: {cleaned[0]}"\n'
        '    preview = ", ".join(cleaned[:2])\n'
        "    if len(cleaned) > 2:\n"
        '        preview += f", +{len(cleaned) - 2} more"\n'
        '    return f"{len(cleaned)} tasks queued: {preview}"\n\n\n'
        "def estimate_focus_minutes(task_count: int, *, per_task: int = 25) -> int:\n"
        '    """Intentional demo bug: a single task still underestimates by five minutes."""\n'
        "    if task_count <= 0:\n"
        "        return 0\n"
        "    return task_count * per_task\n"
    )
    stage["tests/test_demo_app.py"] = (
        "import unittest\n\n"
        "from demo_app import estimate_focus_minutes, summarize_queue\n\n\n"
        "class DemoAppTests(unittest.TestCase):\n"
        "    def test_summarize_queue_handles_empty_input(self) -> None:\n"
        '        self.assertEqual(summarize_queue([]), "No tasks queued.")\n\n'
        "    def test_summarize_queue_shows_preview_and_count(self) -> None:\n"
        '        summary = summarize_queue(["plan onboarding", "write docs", "ship fallback"])\n'
        '        self.assertEqual(summary, "3 tasks queued: plan onboarding, write docs, +1 more")\n\n'
        "    def test_estimate_focus_minutes_uses_default_block(self) -> None:\n"
        "        self.assertEqual(estimate_focus_minutes(3), 75)\n\n"
        "    def test_estimate_focus_minutes_reserves_a_half_hour_for_one_task(self) -> None:\n"
        "        \"\"\"Intentional demo bug: the helper still underestimates a single task.\"\"\"\n"
        "        self.assertEqual(estimate_focus_minutes(1), 30)\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n"
    )
    return stage


def _demo_repo_stage_three_contents() -> dict[str, str]:
    stage = _demo_repo_stage_two_contents()
    stage["README.md"] = (
        "# PollyPM Demo Repo\n\n"
        "This repo is the offline fallback used by onboarding when no recent local\n"
        "projects are detected.\n\n"
        "It is intentionally small, but self-contained:\n\n"
        "- `demo_app.py` contains a tiny queue-summary helper with one deliberate bug.\n"
        "- `tests/test_demo_app.py` contains the matching failing regression test.\n"
        "- `demo_cli.py` is a tiny entrypoint that prints the demo queue summary.\n"
        "- `demo_data.py` holds sample task titles used by the CLI and tests.\n"
        "- `demo_history.md` explains the replayable git history seeded by onboarding.\n"
        "- `TASK.md` describes the seeded PollyPM task that onboarding can add to the\n"
        "  project database.\n\n"
        "## What To Try\n\n"
        "- Run `python -m unittest discover -s tests -p \"test_*.py\"`\n"
        "- Read `TASK.md` and `demo_history.md`\n"
        "- Fix the queue estimate regression, then rerun the demo tests\n"
        "- Ask Polly to explain, extend, or test the queue summary behavior\n\n"
        "Everything in this repo uses only the Python standard library.\n"
    )
    stage["TASK.md"] = (
        "# Demo Task\n\n"
        "Fix the queue estimate regression in `demo_app.py`.\n\n"
        "The follow-up is intentionally small:\n\n"
        "- `estimate_focus_minutes(1)` should reserve a 30-minute block.\n"
        "- The demo test suite already includes a failing regression test.\n"
        "- The repo history is replayable so you can inspect the change in three commits.\n"
        "- Keep the task in PollyPM if you want it to show up in the seeded inbox.\n"
    )
    stage["tests/test_demo_history.py"] = (
        "from pathlib import Path\n\n\n"
        "def test_demo_history_describes_the_seeded_git_log() -> None:\n"
        "    text = Path(\"demo_history.md\").read_text(encoding=\"utf-8\")\n"
        "    lowered = text.lower()\n"
        '    assert "replayable" in lowered\n'
        '    assert "three commits" in lowered or "3 commits" in lowered\n'
    )
    stage["tests/test_demo_task.py"] = (
        "from pathlib import Path\n\n\n"
        "def test_task_doc_points_at_the_seeded_bug_and_tests() -> None:\n"
        "    text = Path(\"TASK.md\").read_text(encoding=\"utf-8\")\n"
        '    assert "demo_app.py" in text\n'
        '    assert "tests/test_demo_app.py" in text\n'
        '    assert "30-minute block" in text\n'
    )
    return stage


def _write_demo_repo_files(target: Path, files: dict[str, str]) -> None:
    for relative_name, contents in files.items():
        destination = target / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")


def _remove_demo_repo_files(target: Path, relative_names: set[str]) -> None:
    for relative_name in relative_names:
        path = target / relative_name
        if path.exists():
            path.unlink()


def seed_demo_project_task(project_path: Path, *, project_key: str) -> str:
    """Seed one small PollyPM task into the demo project's work DB."""
    from pollypm.projects import ensure_project_scaffold
    from pollypm.work import create_work_service

    ensure_project_scaffold(project_path)
    db_path = project_path / ".pollypm" / "state.db"
    with create_work_service(db_path=db_path, project_path=project_path) as svc:
        existing = svc.list_tasks(project=project_key)
        for task in existing:
            if task.title == DEMO_PROJECT_TASK_TITLE:
                return task.task_id
        task = svc.create(
            title=DEMO_PROJECT_TASK_TITLE,
            description=DEMO_PROJECT_TASK_DESCRIPTION,
            type="task",
            project=project_key,
            flow_template="standard",
            roles={"worker": "demo_worker", "reviewer": "demo_reviewer"},
            priority="normal",
            created_by="onboarding",
            labels=["demo", "onboarding"],
        )
        svc.queue(task.task_id, "onboarding", skip_gates=True)
        return task.task_id


def _init_demo_project_git(target: Path) -> None:
    if shutil.which("git") is None or (target / ".git").exists():
        return
    subprocess.run(
        ["git", "init", "-q", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )


def _seed_demo_project_git_history(target: Path) -> None:
    git = ["git", "-C", str(target)]
    subprocess.run([*git, "config", "user.name", "PollyPM Demo"], check=False, capture_output=True, text=True)
    subprocess.run([*git, "config", "user.email", "demo@pollypm.local"], check=False, capture_output=True, text=True)

    stage_one = _demo_repo_stage_one_contents()
    _write_demo_repo_files(target, stage_one)
    _remove_demo_repo_files(target, DEMO_PROJECT_REPLAY_TASK_FILES)
    subprocess.run([*git, "add", "-A"], check=False, capture_output=True, text=True)
    subprocess.run(
        [*git, "commit", "-q", "-m", "demo: scaffold offline fallback"],
        check=False,
        capture_output=True,
        text=True,
    )

    stage_two = _demo_repo_stage_two_contents()
    _write_demo_repo_files(target, stage_two)
    subprocess.run([*git, "add", "-A"], check=False, capture_output=True, text=True)
    subprocess.run(
        [*git, "commit", "-q", "-m", "demo: introduce the queue bug"],
        check=False,
        capture_output=True,
        text=True,
    )

    stage_three = _demo_repo_stage_three_contents()
    _write_demo_repo_files(target, stage_three)
    subprocess.run([*git, "add", "-A"], check=False, capture_output=True, text=True)
    subprocess.run(
        [*git, "commit", "-q", "-m", "demo: add task notes and replay guide"],
        check=False,
        capture_output=True,
        text=True,
    )


def _demo_project_history_seeded(target: Path) -> bool:
    if not (target / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(target), "rev-list", "--count", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip() or "0") >= DEMO_PROJECT_REPLAY_COMMIT_COUNT
    except ValueError:
        return False


def provision_demo_project_fallback(config_path: Path) -> Path:
    target = demo_project_fallback_destination(config_path)
    marker = target / DEMO_PROJECT_MARKER
    if not marker.exists():
        target.mkdir(parents=True, exist_ok=True)
        _write_demo_project_files(target)
    _init_demo_project_git(target)
    if shutil.which("git") is not None and not _demo_project_history_seeded(target):
        _seed_demo_project_git_history(target)
    ensure_project_scaffold(target)
    return target


def add_selected_projects(config_path: Path, selected_paths: list[Path]) -> list[KnownProject]:
    if not selected_paths:
        return []
    config = load_config(config_path)
    added: list[KnownProject] = []
    for repo_path in selected_paths:
        normalized = repo_path.resolve()
        if any(project.path.resolve() == normalized for project in config.projects.values()):
            continue
        project = KnownProject(
            key=make_project_key(normalized, set(config.projects) | {item.key for item in added}),
            path=normalized,
            name=normalized.name,
            kind=ProjectKind.GIT,
        )
        config.projects[project.key] = project
        ensure_project_scaffold(normalized)
        # Commit the freshly written .gitignore/docs/issues so the
        # project root is clean before the user runs their first task
        # (#926). Best-effort: a failure here is logged and the
        # registration still succeeds.
        try:
            commit_initial_scaffold(normalized)
        except Exception:  # noqa: BLE001
            pass
        added.append(project)
    if added:
        write_config(config, path=config_path, force=True)
    return added


def _slugify_email(provider: ProviderKind, email: str) -> str:
    normalized = email.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return f"{provider.value}_{slug}"


def _display_label(account: ConnectedAccount) -> str:
    provider = "Claude" if account.provider is ProviderKind.CLAUDE else "Codex"
    return f"{provider} · {account.email}"


def _choose_account(accounts: dict[str, ConnectedAccount], prompt: str) -> str:
    ordered_names = list(accounts)
    typer.echo("")
    typer.echo("Available accounts:")
    for index, name in enumerate(ordered_names, start=1):
        typer.echo(f"{index}. {_display_label(accounts[name])}")
    choice = typer.prompt(prompt, type=int, default=1)
    if choice < 1 or choice > len(ordered_names):
        raise typer.BadParameter(f"Choose a number between 1 and {len(ordered_names)}.")
    return ordered_names[choice - 1]


def _default_failover_accounts(accounts: dict[str, ConnectedAccount], controller_account: str) -> list[str]:
    remaining = [name for name in accounts if name != controller_account]
    if not remaining:
        return []
    typer.echo("")
    typer.echo("PollyPM failover order:")
    for name in remaining:
        typer.echo(f"- {_display_label(accounts[name])}")
    return remaining


def _runtime_home(root_dir: Path, provider: ProviderKind, index: int) -> Path:
    return root_dir / "homes" / f"onboarding_{provider.value}_{index}"


def _login_command(
    provider: ProviderKind,
    *,
    interactive: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    """Dispatch the login shell snippet to the provider package.

    Onboarding stays provider-agnostic: it folds the Codex-only
    ``codex_headless`` preference into the generic ``headless`` kwarg
    the Protocol exposes, then asks the registered adapter to render
    its own command.
    """
    from pollypm.acct.registry import get_provider

    headless = preferences is not None and preferences.codex_headless
    return get_provider(provider.value).login_command(
        interactive=interactive,
        headless=headless,
    )


def _build_login_shell(
    provider: ProviderKind,
    home: Path,
    *,
    return_to_caller: bool = False,
    interactive: bool = False,
    force_fresh_auth: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    env = provider_profile_env_for_provider(provider, home)
    parts = [f"mkdir -p {shlex.quote(str(home))}"]
    if "CODEX_HOME" in env:
        parts.append(f"mkdir -p {shlex.quote(env['CODEX_HOME'])}")
    if "CLAUDE_CONFIG_DIR" in env:
        parts.append(f"mkdir -p {shlex.quote(env['CLAUDE_CONFIG_DIR'])}")
    for key, value in env.items():
        parts.append(f"export {key}={shlex.quote(value)}")
    if force_fresh_auth:
        from pollypm.acct.registry import get_provider

        parts.append(get_provider(provider.value).logout_command())
    parts.append(_login_command(provider, interactive=interactive, preferences=preferences))
    if return_to_caller:
        parts.append('printf "\\nPollyPM: login window complete. Returning to onboarding...\\n"')
        parts.append("sleep 1")
    else:
        parts.append('printf "\\nPollyPM: login window complete. Detach with Ctrl-b d to continue onboarding.\\n"')
        parts.append('exec "${SHELL:-/bin/zsh}" -l')
    return "sh -lc " + shlex.quote(" && ".join(parts))


def _decode_jwt_payload(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _detect_account_email(provider: ProviderKind, home: Path) -> str | None:
    """Dispatch email-from-home detection to the provider package.

    Tiny glue for the login-window loop in this module — the real
    detectors live in :mod:`pollypm.providers.claude.detect` and
    :mod:`pollypm.providers.codex.detect`. Kept here so the loop's
    provider-agnostic flow (capture pane → try email → try home) has a
    single entry point tests can monkeypatch.
    """
    from pollypm.acct.registry import get_provider

    return get_provider(provider.value).detect_email(
        AccountConfig(name=f"{provider.value}_probe", provider=provider, home=home),
    )


def _detect_email_from_pane(provider: ProviderKind, pane_text: str) -> str | None:
    """Dispatch pane-scraping to the provider package.

    Mirror of :func:`_detect_account_email` — the heavy lifting happens
    in the provider ``detect`` modules; this dispatcher exists so the
    login-window loop can stay provider-agnostic.
    """
    from pollypm.acct.registry import get_provider

    return get_provider(provider.value).detect_email_from_pane(pane_text)


def _login_completion_marker_seen(pane_text: str, provider: ProviderKind | None = None) -> bool:
    """Dispatch the pane-marker check to the provider package.

    ``provider`` is optional so the legacy single-arg call shape kept
    by tests (and the default ``PollyPM: login window complete.``
    marker) continues to work. When ``provider`` is omitted we fall
    back to the shared marker — both built-in providers accept it,
    and a third-party provider that needs a richer check will be
    asked through its registered adapter from the wait loop below.
    """
    if provider is None:
        return "PollyPM: login window complete." in pane_text
    from pollypm.acct.registry import get_provider

    return get_provider(provider.value).login_completion_marker_seen(pane_text)


def _wait_for_login_completion(
    tmux: TmuxClient,
    *,
    target: str,
    provider: ProviderKind,
    home: Path,
    allow_existing_auth_shortcut: bool = True,
    timeout_seconds: int = 300,
    poll_interval: float = 1.0,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_pane = ""
    while time.monotonic() < deadline:
        try:
            last_pane = tmux.capture_pane(target, lines=200)
        except Exception:  # noqa: BLE001
            last_pane = ""

        if _login_completion_marker_seen(last_pane, provider):
            return True, last_pane

        if _detect_email_from_pane(provider, last_pane):
            return True, last_pane
        if allow_existing_auth_shortcut and _detect_account_email(provider, home):
            return True, last_pane

        time.sleep(poll_interval)

    return False, last_pane


def _final_account_home(root_dir: Path, account_name: str) -> Path:
    return root_dir / "homes" / account_name


def _promote_onboarding_home(temp_home: Path, final_home: Path) -> Path:
    if temp_home.resolve() == final_home.resolve():
        return final_home

    final_home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if final_home.exists():
        shutil.rmtree(temp_home, ignore_errors=True)
        return final_home
    shutil.move(str(temp_home), str(final_home))
    return final_home


def _provider_from_home_name(name: str) -> ProviderKind | None:
    if name.startswith("codex_") or name.startswith("onboarding_codex_"):
        return ProviderKind.CODEX
    if name.startswith("claude_") or name.startswith("onboarding_claude_"):
        return ProviderKind.CLAUDE
    return None


def _recover_existing_accounts(root_dir: Path) -> dict[str, ConnectedAccount]:
    homes_dir = root_dir / "homes"
    if not homes_dir.exists():
        return {}

    connected: dict[str, ConnectedAccount] = {}
    for entry in sorted(homes_dir.iterdir()):
        if not entry.is_dir():
            continue
        provider = _provider_from_home_name(entry.name)
        if provider is None:
            continue

        email = _detect_account_email(provider, entry)
        if email is None:
            continue

        account_name = _slugify_email(provider, email)
        final_home = _final_account_home(root_dir, account_name)
        actual_home = entry
        if entry.name.startswith("onboarding_"):
            actual_home = _promote_onboarding_home(entry, final_home)
        from pollypm.acct.registry import get_provider

        get_provider(provider.value).prime_home(
            actual_home if actual_home.exists() else final_home,
        )
        connected[account_name] = ConnectedAccount(
            provider=provider,
            email=email,
            account_name=account_name,
            home=actual_home if actual_home.exists() else final_home,
        )

    return connected


def _run_login_window(
    tmux: TmuxClient,
    *,
    provider: ProviderKind,
    home: Path,
    window_label: str,
    quiet: bool = False,
    allow_existing_auth_shortcut: bool = True,
    force_fresh_auth: bool = False,
    preferences: LoginPreferences | None = None,
) -> str:
    current_tmux = tmux.current_session_name()
    temp_session = f"pollypm-login-{window_label}"

    if current_tmux:
        current_window = tmux.current_window_index()
        tmux.run("kill-window", "-t", f"{current_tmux}:{window_label}", check=False)
        tmux.create_window(
            current_tmux,
            window_label,
            _build_login_shell(
                provider,
                home,
                interactive=(provider is ProviderKind.CLAUDE),
                force_fresh_auth=force_fresh_auth,
                preferences=preferences,
            ),
        )
        tmux.select_window(f"{current_tmux}:{window_label}")
        if not quiet:
            typer.echo(
                f"Finish login in window `{window_label}`. PollyPM will switch you back automatically "
                "when the login completes."
            )
        completed, pane_text = _wait_for_login_completion(
            tmux,
            target=f"{current_tmux}:{window_label}",
            provider=provider,
            home=home,
            allow_existing_auth_shortcut=allow_existing_auth_shortcut,
        )
        if current_window is not None:
            tmux.select_window(f"{current_tmux}:{current_window}")
        if not completed:
            if not quiet:
                typer.echo("PollyPM did not detect completion automatically.")
                typer.echo(f"Finish login in window `{window_label}`, then press Enter here to continue.")
                typer.prompt("Press Enter after login is complete", default="", show_default=False)
            pane_text = tmux.capture_pane(f"{current_tmux}:{window_label}", lines=200)
        elif not quiet:
            typer.echo("Login completed. Returning to onboarding.")
        tmux.run("kill-window", "-t", f"{current_tmux}:{window_label}", check=False)
        return pane_text

    if tmux.has_session(temp_session):
        tmux.kill_session(temp_session)
    if not quiet:
        typer.echo("Complete login in that tmux session. PollyPM will return here automatically.")
    tmux.create_session(
        temp_session,
        "login",
        _build_login_shell(
            provider,
            home,
            interactive=(provider is ProviderKind.CLAUDE),
            force_fresh_auth=force_fresh_auth,
            preferences=preferences,
        ),
        remain_on_exit=False,
    )
    watch_result: dict[str, str | bool] = {"pane_text": "", "completed": False}

    def _watch_for_completion() -> None:
        completed, pane_text = _wait_for_login_completion(
            tmux,
            target=f"{temp_session}:0",
            provider=provider,
            home=home,
            allow_existing_auth_shortcut=allow_existing_auth_shortcut,
        )
        watch_result["completed"] = completed
        watch_result["pane_text"] = pane_text
        if completed and tmux.has_session(temp_session):
            tmux.kill_session(temp_session)

    watcher = threading.Thread(target=_watch_for_completion, daemon=True)
    watcher.start()
    raise_code = tmux.attach_session(temp_session)
    watcher.join(timeout=1.0)
    if raise_code != 0:
        if tmux.has_session(temp_session):
            tmux.kill_session(temp_session)
        raise LoginCancelled("Login cancelled. Returned to onboarding.")

    pane_text = str(watch_result.get("pane_text") or "")
    if tmux.has_session(temp_session):
        if not pane_text:
            pane_text = tmux.capture_pane(f"{temp_session}:0", lines=200)
        tmux.kill_session(temp_session)
    return pane_text


def _connect_account_via_tmux(
    tmux: TmuxClient,
    *,
    root_dir: Path,
    provider: ProviderKind,
    index: int,
    quiet: bool = False,
    preferences: LoginPreferences | None = None,
) -> ConnectedAccount:
    label = "Codex" if provider is ProviderKind.CODEX else "Claude"
    home = _runtime_home(root_dir, provider, index)
    temp_window = f"onboard-{provider.value}-{index}"

    if not quiet:
        typer.echo("")
        typer.echo(f"Opening a temporary login window for {label} account #{index}.")
    pane_text = _run_login_window(
        tmux,
        provider=provider,
        home=home,
        window_label=temp_window,
        quiet=quiet,
        preferences=preferences,
    )

    email = _detect_email_from_pane(provider, pane_text) or _detect_account_email(provider, home)
    if provider is ProviderKind.CLAUDE and email is None:
        message = (
            "Claude login finished, but the managed PollyPM profile is still not authenticated. "
            "This usually means Claude completed browser auth without persisting credentials into the "
            "managed CLAUDE_CONFIG_DIR profile."
        )
        if quiet:
            raise typer.BadParameter(message)
        raise typer.BadParameter(message)
    if email is None:
        if quiet:
            raise typer.BadParameter(
                "PollyPM could not auto-detect the connected account email. "
                "Try running the login again and, for Codex, open `/status` once login completes."
            )
        typer.echo("")
        typer.echo("PollyPM could not auto-detect the account email from the completed login.")
        email = typer.prompt(f"Email address for this {label} account").strip().lower()

    if provider is ProviderKind.CLAUDE:
        # Claude auth lives in the macOS Keychain, keyed to the CLAUDE_CONFIG_DIR path hash.
        # Renaming the home directory would invalidate the keychain entry, so keep it in place.
        final_home = home
        _prime_claude_home(final_home)
    else:
        final_home = _promote_onboarding_home(
            home,
            _final_account_home(root_dir, _slugify_email(provider, email)),
        )

    return ConnectedAccount(
        provider=provider,
        email=email,
        account_name=_slugify_email(provider, email),
        home=final_home,
    )


def _resolve_account_identifier(config: PollyPMConfig, identifier: str) -> tuple[str, AccountConfig]:
    if identifier in config.accounts:
        return identifier, config.accounts[identifier]

    lowered = identifier.strip().lower()
    matches = [
        (name, account)
        for name, account in config.accounts.items()
        if account.email and account.email.lower() == lowered
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise typer.BadParameter(f"Multiple accounts matched {identifier}; use the internal account key instead.")
    raise typer.BadParameter(f"Unknown account: {identifier}")


def build_onboarded_config(
    *,
    root_dir: Path,
    accounts: dict[str, ConnectedAccount],
    controller_account: str,
    open_permissions_by_default: bool = True,
    failover_enabled: bool,
    failover_accounts: list[str],
    projects: dict[str, KnownProject] | None = None,
) -> PollyPMConfig:
    if controller_account not in accounts:
        raise ValueError(f"Unknown controller account: {controller_account}")

    controller = accounts[controller_account]
    base_dir = root_dir / ".pollypm"

    config_accounts = {
        name: AccountConfig(
            name=account.account_name,
            provider=account.provider,
            email=account.email,
            home=account.home,
        )
        for name, account in accounts.items()
    }

    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=root_dir,
            tmux_session="pollypm",
            workspace_root=DEFAULT_WORKSPACE_ROOT,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=base_dir / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account=controller_account,
            open_permissions_by_default=open_permissions_by_default,
            failover_enabled=failover_enabled,
            failover_accounts=failover_accounts,
        ),
        accounts=config_accounts,
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=controller.provider,
                account=controller.account_name,
                cwd=root_dir,
                project="pollypm",
                window_name="pm-heartbeat",
                prompt=heartbeat_prompt(),
                agent_profile="heartbeat",
                args=default_control_args(controller.provider, open_permissions=open_permissions_by_default, role="heartbeat-supervisor"),
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=controller.provider,
                account=controller.account_name,
                cwd=root_dir,
                project="pollypm",
                window_name="pm-operator",
                prompt=polly_prompt(),
                agent_profile="polly",
                args=default_control_args(controller.provider, open_permissions=open_permissions_by_default, role="operator-pm"),
            ),
        },
        projects=dict(projects or {}),
    )

def _seeded_demo_route(result: OnboardingResult) -> str | None:
    project_key = result.seeded_demo_project_key
    task_id = result.seeded_demo_task_id
    if not project_key or not task_id:
        return None
    task_num = task_id.rsplit("/", 1)[-1].rsplit(":", 1)[-1].strip()
    if not task_num:
        return None
    return f"project:{project_key}:task:{task_num}"


def _launch_onboarding_experience(result: OnboardingResult) -> bool:
    """Best-effort launch of the seeded demo cockpit after onboarding consent."""
    from pollypm.cockpit_rail import CockpitRouter
    from pollypm.service_api import PollyPMService
    from pollypm.transcript_ingest import start_transcript_ingestion

    service = PollyPMService(result.config_path)
    supervisor = service.load_supervisor()
    session_name = supervisor.config.project.tmux_session
    launched = False

    try:
        if supervisor.tmux.has_session(session_name):
            supervisor.ensure_layout()
        else:
            supervisor.bootstrap_tmux(skip_probe=True)
    except Exception:  # noqa: BLE001
        pass

    try:
        start_transcript_ingestion(supervisor.config)
    except Exception:  # noqa: BLE001
        pass

    router = CockpitRouter(result.config_path)
    try:
        router.ensure_cockpit_layout()
        time.sleep(0.3)
        supervisor.start_cockpit_tui(session_name)
    except Exception:  # noqa: BLE001
        pass

    route_key = _seeded_demo_route(result)
    if route_key is not None:
        try:
            router.route_selected(route_key)
        except Exception:  # noqa: BLE001
            pass

    try:
        subprocess.run(
            ["uv", "tool", "install", "--editable", "--reinstall", str(result.parent)],
            cwd=result.parent,
            check=False,
            text=True,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        current_tmux = create_tmux_client().current_session_name()
    except Exception:  # noqa: BLE001
        current_tmux = None
    if current_tmux == session_name:
        launched = True
        try:
            supervisor.focus_console()
        except Exception:  # noqa: BLE001
            pass
        return launched
    try:
        supervisor.tmux.attach_session(session_name)
        launched = True
    except Exception:  # noqa: BLE001
        pass
    return launched


def _account_ready_for_welcome_back(account: AccountConfig) -> bool:
    if account.home is None:
        detected = _detected_host_account(account.provider)
        return detected is not None and detected.email.lower() == (account.email or "").lower()
    return account.home.exists()


def _render_welcome_back_summary(config: PollyPMConfig) -> list[str]:
    lines = ["Welcome back.", "", "Accounts:"]
    for name, account in config.accounts.items():
        ok = _account_ready_for_welcome_back(account)
        label = account.email or name
        mode = "default profile" if account.home is None else "isolated home"
        lines.append(f"- {label} [{account.provider.value}] ({mode}) {'ok' if ok else 'needs re-login'}")
    if config.projects:
        lines.extend(["", "Projects:"])
        for project in config.projects.values():
            lines.append(f"- {project.display_label()} -> {project.path}")
    return lines


def run_onboarding(
    config_path: Path = DEFAULT_CONFIG_PATH,
    force: bool = False,
    *,
    no_animation: bool = False,
) -> OnboardingResult:
    from pollypm.onboarding_tui import run_onboarding_app

    if not force and config_path.exists():
        try:
            config = load_config(config_path)
        except Exception:  # noqa: BLE001
            config = None
        if config is not None:
            for line in _render_welcome_back_summary(config):
                typer.echo(line)
            typer.echo("")
            typer.echo("1. Open cockpit")
            typer.echo("2. Add another account")
            typer.echo("3. Re-run full onboarding")
            choice = typer.prompt("Choose", default="1")
            if choice == "1":
                result = OnboardingResult(config_path=config_path, launch_requested=True)
                if _launch_onboarding_experience(result):
                    raise typer.Exit()
                return result
            if choice == "2":
                result = run_onboarding_app(config_path=config_path, force=False, no_animation=no_animation)
                if result.launch_requested and _launch_onboarding_experience(result):
                    raise typer.Exit()
                return result
            if choice == "3":
                result = run_onboarding_app(config_path=config_path, force=True, no_animation=no_animation)
                if result.launch_requested and _launch_onboarding_experience(result):
                    raise typer.Exit()
                return result

    result = run_onboarding_app(config_path=config_path, force=force, no_animation=no_animation)
    if result.launch_requested and _launch_onboarding_experience(result):
        raise typer.Exit()
    return result


def relogin_account(config_path: Path, identifier: str) -> tuple[str, str]:
    config = load_config(config_path)
    account_name, account = _resolve_account_identifier(config, identifier)
    if account.home is None:
        raise typer.BadParameter(f"Account {account_name} does not have an isolated home configured.")

    tmux = create_tmux_client()
    typer.echo(f"Re-launching login for {account.email or account_name} [{account.provider.value}]")
    pane_text = _run_login_window(
        tmux,
        provider=account.provider,
        home=account.home,
        window_label=f"relogin-{account_name}",
    )

    detected_email = _detect_email_from_pane(account.provider, pane_text) or _detect_account_email(
        account.provider,
        account.home,
    )
    if detected_email and detected_email != (account.email or "").lower():
        config.accounts[account_name].email = detected_email
        write_config(config, path=config_path, force=True)
        return account_name, detected_email

    return account_name, account.email or detected_email or account_name
