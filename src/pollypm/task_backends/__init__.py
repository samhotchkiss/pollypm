from pathlib import Path
import tomllib

from pollypm.task_backends.base import TaskBackend, TaskRecord
from pollypm.task_backends.file import FileTaskBackend


def _project_task_backend_settings(project_path: Path) -> tuple[str, str | None]:
    from pollypm.projects import project_config_dir

    config_path = project_config_dir(project_path) / "project.toml"
    if not config_path.exists():
        return "file", None
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "file", None
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        return "file", None
    backend_name = str(plugins.get("issue_backend", "file"))
    github_settings = plugins.get("github_issues")
    repo = github_settings.get("repo") if isinstance(github_settings, dict) else None
    return backend_name, str(repo) if isinstance(repo, str) and repo.strip() else None


def get_task_backend(project_path: Path, backend_name: str | None = None, *, repo: str | None = None) -> TaskBackend:
    resolved_backend, resolved_repo = _project_task_backend_settings(project_path)
    backend_name = backend_name or resolved_backend
    repo = repo or resolved_repo
    if backend_name == "file":
        return FileTaskBackend(project_path)
    if backend_name == "github":
        from pollypm.task_backends.github import GitHubTaskBackend
        return GitHubTaskBackend(project_path, repo=repo)
    raise ValueError(f"Unsupported task backend: {backend_name}")


__all__ = ["TaskBackend", "TaskRecord", "FileTaskBackend", "get_task_backend"]
