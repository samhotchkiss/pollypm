import base64
import json
import shlex
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor


def _decode_launch_payload(command: str) -> dict[str, object]:
    parts = shlex.split(command)
    if parts[0] == "sh" and "-lc" in parts:
        parts = shlex.split(parts[-1])
    payload = parts[-1]
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return json.loads(raw.decode("utf-8"))


def test_worker_launches_use_project_or_worktree_cwd_while_auth_stays_in_account_home(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worktree_root = project_root / ".pollypm" / "worktrees" / "review_demo" / "demo-pa-review_demo"
    project_root.mkdir()
    worktree_root.mkdir(parents=True)
    account_home = tmp_path / ".pollypm" / "homes" / "codex_worker"
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="codex_worker"),
        accounts={
            "codex_worker": AccountConfig(
                name="codex_worker",
                provider=ProviderKind.CODEX,
                runtime=RuntimeKind.LOCAL,
                home=account_home,
            )
        },
        sessions={
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CODEX,
                account="codex_worker",
                cwd=project_root,
                project="demo",
                window_name="worker-demo",
            ),
            "review_demo": SessionConfig(
                name="review_demo",
                role="review",
                provider=ProviderKind.CODEX,
                account="codex_worker",
                cwd=worktree_root,
                project="demo",
                window_name="review-demo",
            ),
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    launches = {launch.session.name: launch for launch in Supervisor(config).plan_launches()}
    worker_payload = _decode_launch_payload(launches["worker_demo"].command)
    review_payload = _decode_launch_payload(launches["review_demo"].command)

    assert worker_payload["cwd"] == str(project_root)
    assert review_payload["cwd"] == str(worktree_root)
    assert worker_payload["cwd"] != review_payload["cwd"]
    assert worker_payload["codex_home"] == str(account_home / ".codex")
    assert review_payload["codex_home"] == str(account_home / ".codex")
    assert worker_payload["home"] == str(account_home)
    assert review_payload["home"] == str(account_home)
