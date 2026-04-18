import json
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, PollyPMConfig, PollyPMSettings, ProjectKind, ProjectSettings, ProviderKind
from pollypm.service_api import PollyPMService


def test_github_issue_pipeline_rejects_then_approves(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
            )
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
                tracked=True,
            )
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    config_dir = project_root / ".pollypm" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project.toml").write_text(
        """
[project]
display_name = "Demo"

[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    issue = {
        "number": 42,
        "title": "Wire the backend",
        "body": "Implement the gh-backed tracker.",
        "label": "polly:ready",
        "closed": False,
    }
    comments: list[str] = []

    def fake_gh(*args: str, check: bool = True):
        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        if args[:2] == ("issue", "create"):
            issue["title"] = args[args.index("--title") + 1]
            issue["body"] = args[args.index("--body") + 1]
            issue["label"] = args[args.index("--label") + 1]
            issue["closed"] = False
            return Result("https://github.com/acme/widgets/issues/42\n")
        if args[:2] == ("issue", "list"):
            label = args[args.index("--label") + 1] if "--label" in args else None
            matches = []
            if label is None or issue["label"] == label:
                matches.append(
                    {
                        "number": issue["number"],
                        "title": issue["title"],
                        "state": "CLOSED" if issue["closed"] else "OPEN",
                    }
                )
            if "-q" in args:
                return Result(str(len(matches)))
            return Result(json.dumps(matches))
        if args[:2] == ("issue", "view"):
            if "--json" in args and "comments" in args:
                payload = {
                    "comments": [
                        {"author": {"login": "polly"}, "body": body}
                        for body in comments
                    ]
                }
                return Result(json.dumps(payload))
            fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
            payload: dict[str, object] = {}
            if "number" in fields:
                payload["number"] = issue["number"]
            if "title" in fields:
                payload["title"] = issue["title"]
            if "body" in fields:
                payload["body"] = issue["body"]
            if "labels" in fields:
                payload["labels"] = [{"name": issue["label"]}]
            return Result(json.dumps(payload))
        if args[:2] == ("issue", "edit") and "--remove-label" in args:
            if issue["label"] == args[args.index("--remove-label") + 1]:
                issue["label"] = ""
            return Result()
        if args[:2] == ("issue", "edit") and "--add-label" in args:
            issue["label"] = args[args.index("--add-label") + 1]
            return Result()
        if args[:2] == ("issue", "comment"):
            comments.append(args[args.index("--body") + 1])
            return Result()
        if args[:2] == ("issue", "close"):
            issue["closed"] = True
            return Result()
        if args[:2] == ("issue", "reopen"):
            issue["closed"] = False
            return Result()
        raise AssertionError(f"Unexpected gh call: {args}")

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)
    service = PollyPMService(config_path)

    created = service.create_task("demo", title="Wire the backend", body="Implement the gh-backed tracker.")
    next_task = service.next_available_task("demo")
    assert created.task_id == "42"
    assert next_task is not None
    assert next_task.task_id == "42"

    service.move_task("demo", "42", to_state="02-in-progress")
    service.append_task_handoff(
        "demo",
        "42",
        what_done="Implemented the GitHub issue flow.",
        how_to_test="Run the targeted pytest suite.",
        branch_or_pr="https://github.com/acme/widgets/pull/42",
        deviations="None.",
    )
    service.move_task("demo", "42", to_state="03-needs-review")

    rejected = service.review_task(
        "demo",
        "42",
        approved=False,
        summary="Needs one more regression test.",
        verification="Reviewed the issue history independently.",
        changes_requested="Add reject-loop coverage.",
    )
    assert rejected.state == "02-in-progress"

    service.append_task_handoff(
        "demo",
        "42",
        what_done="Added reject-loop coverage.",
        how_to_test="Run the integration and CLI suites.",
        branch_or_pr="https://github.com/acme/widgets/pull/42",
        deviations="None.",
    )
    service.move_task("demo", "42", to_state="03-needs-review")

    approved = service.review_task(
        "demo",
        "42",
        approved=True,
        summary="Looks correct.",
        verification="Ran the issue workflow independently.",
    )
    history = service.task_history("demo", "42")
    counts = service.task_state_counts("demo")

    assert approved.state == "05-completed"
    assert any("## Handoff" in entry for entry in history)
    assert any("### Branch / PR" in entry for entry in history)
    assert any("### Change Requests" in entry for entry in history)
    assert any("### Independent Verification" in entry for entry in history)
    assert counts["05-completed"] == 1
    assert counts["02-in-progress"] == 0
