from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from pollypm.config import PROJECT_CONFIG_DIRNAME, load_config, write_config
from pollypm.doc_scaffold import scaffold_docs
from pollypm.models import KnownProject, ProjectKind
from pollypm.task_backends import FileTaskBackend, get_task_backend


DEFAULT_WORKSPACE_ROOT = Path.home() / "dev"
DEFAULT_SCAN_ROOT = Path.home()
TRACKER_TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "issue-tracker.md"
PROJECT_INSTRUCTIONS_TEMPLATE = Path(__file__).resolve().parents[2] / ".pollypm" / "INSTRUCT.md"
PACKAGED_INSTRUCTIONS_TEMPLATE = (
    Path(__file__).resolve().parent / "defaults" / "docs" / "INSTRUCT.md.template"
)
LEGACY_INSTRUCTIONS_LEAD = "Test and operate PollyPM through Polly chat"
SKIP_DIR_NAMES = {
    ".cache",
    ".cargo",
    ".local",
    ".npm",
    ".pnpm-store",
    ".pollypm",
    ".rustup",
    ".Trash",
    ".venv",
    "Applications",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "node_modules",
    "vendor",
}

PERSONA_NAMES_BY_INITIAL = {
    "a": "Ada",
    "b": "Bea",
    "c": "Cole",
    "d": "Dora",
    "e": "Eli",
    "f": "Finn",
    "g": "Gia",
    "h": "Hugo",
    "i": "Iris",
    "j": "June",
    "k": "Kai",
    "l": "Lena",
    "m": "Milo",
    "n": "Nora",
    "o": "Olive",
    "p": "Pete",
    "q": "Quinn",
    "r": "Ruby",
    "s": "Sage",
    "t": "Theo",
    "u": "Uma",
    "v": "Vera",
    "w": "Wren",
    "x": "Xena",
    "y": "Yara",
    "z": "Zoe",
}


def slugify_project_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "project"


def normalize_project_path(path: Path) -> Path:
    return path.expanduser().resolve()


def default_persona_name(project_name: str) -> str:
    for char in project_name.strip().casefold():
        if char.isalpha():
            return PERSONA_NAMES_BY_INITIAL.get(char, "Pete")
    return "Pete"


def detect_project_kind(path: Path) -> ProjectKind:
    normalized = normalize_project_path(path)
    return ProjectKind.GIT if (normalized / ".git").exists() else ProjectKind.FOLDER


def project_pollypm_dir(project_path: Path) -> Path:
    return normalize_project_path(project_path) / ".pollypm"


def project_instruction_dir(project_path: Path) -> Path:
    return normalize_project_path(project_path) / ".pollypm"


def project_instruction_file(project_path: Path) -> Path:
    return project_instruction_dir(project_path) / "INSTRUCT.md"


def project_dossier_dir(project_path: Path) -> Path:
    return project_pollypm_dir(project_path) / "dossier"


def project_logs_dir(project_path: Path) -> Path:
    return project_instruction_dir(project_path) / "logs"


def project_artifacts_dir(project_path: Path) -> Path:
    return project_instruction_dir(project_path) / "artifacts"


def project_checkpoints_dir(project_path: Path) -> Path:
    return project_artifacts_dir(project_path) / "checkpoints"


def project_worktrees_dir(project_path: Path) -> Path:
    return project_instruction_dir(project_path) / "worktrees"


def project_transcripts_dir(project_path: Path) -> Path:
    return project_instruction_dir(project_path) / "transcripts"


def session_scoped_dir(base_dir: Path, session_id: str) -> Path:
    return base_dir / session_id


def _session_lock_filename(session_id: str) -> str:
    safe = session_id.replace("/", "-").replace("\\", "-")
    return f".session.{safe}.lock"


def session_lock_path(base_dir: Path, session_id: str) -> Path:
    return base_dir / _session_lock_filename(session_id)


_LOCK_STALE_SECONDS = 1800  # 30 minutes — locks older than this are considered stale


def _read_session_lock_payload(lock_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(lock_path.read_text())
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def ensure_session_lock(base_dir: Path, session_id: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_lock_path(base_dir, session_id)
    payload = {"session_id": session_id, "created_at": datetime.now(UTC).isoformat()}
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing = _read_session_lock_payload(lock_path)
        existing_session = existing.get("session_id")
        if isinstance(existing_session, str) and existing_session == session_id:
            return lock_path
        # Check if the lock is stale (older than 30 minutes)
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(existing.get("created_at", ""))).total_seconds()
        except (ValueError, TypeError):
            age = None
        if age is not None and age > _LOCK_STALE_SECONDS:
            lock_path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Session lock conflict at {base_dir}: owned by {existing_session or 'unknown'}")
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            existing = _read_session_lock_payload(lock_path)
            existing_session = existing.get("session_id")
            if isinstance(existing_session, str) and existing_session == session_id:
                return lock_path
            raise RuntimeError(
                f"Session lock conflict at {base_dir}: owned by {existing_session or 'unknown'}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Could not create session lock at {base_dir}: {exc}"
            ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Could not create session lock at {base_dir}: {exc}"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return lock_path


def release_session_lock(base_dir: Path, session_id: str | None = None) -> None:
    if session_id is None:
        lock_paths = list(base_dir.glob(".session*.lock"))
    else:
        lock_paths = [session_lock_path(base_dir, session_id)]
    for lock_path in lock_paths:
        if not lock_path.exists():
            continue
        try:
            existing = json.loads(lock_path.read_text())
        except Exception:  # noqa: BLE001
            existing = {}
        # The try only wraps json.loads — guard the subsequent .get
        # against a parsed-but-non-dict shape (list/null/string) so a
        # corrupted lock file doesn't AttributeError out of the
        # release_session_locks loop.
        if not isinstance(existing, dict):
            existing = {}
        existing_session = existing.get("session_id")
        if (
            session_id is not None
            and isinstance(existing_session, str)
            and existing_session
            and existing_session != session_id
        ):
            continue
        lock_path.unlink(missing_ok=True)
    try:
        if base_dir.exists() and not any(base_dir.iterdir()):
            base_dir.rmdir()
    except OSError:
        pass


def project_issues_dir(project_path: Path) -> Path:
    return get_task_backend(project_path).issues_root()


def ensure_project_scaffold(project_path: Path) -> Path:
    pollypm_dir = project_pollypm_dir(project_path)
    for directory in [
        project_instruction_dir(project_path),
        normalize_project_path(project_path) / PROJECT_CONFIG_DIRNAME,
        project_instruction_dir(project_path) / "rules",
        project_instruction_dir(project_path) / "magic",
        project_transcripts_dir(project_path),
        pollypm_dir,
        project_dossier_dir(project_path),
        project_logs_dir(project_path),
        project_artifacts_dir(project_path),
        project_checkpoints_dir(project_path),
        project_worktrees_dir(project_path),
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    instructions_target = project_instruction_file(project_path)
    if not instructions_target.exists():
        if PROJECT_INSTRUCTIONS_TEMPLATE.exists():
            shutil.copyfile(PROJECT_INSTRUCTIONS_TEMPLATE, instructions_target)
        elif PACKAGED_INSTRUCTIONS_TEMPLATE.exists():
            content = PACKAGED_INSTRUCTIONS_TEMPLATE.read_text(encoding="utf-8")
            if LEGACY_INSTRUCTIONS_LEAD not in content:
                content = f"{LEGACY_INSTRUCTIONS_LEAD}\n\n{content}"
            instructions_target.write_text(content, encoding="utf-8")
    # Generate system reference docs for agent consumption
    scaffold_docs(normalize_project_path(project_path))
    _ensure_gitignore_entry(normalize_project_path(project_path), ".pollypm/")
    # #763 — Materialize built-in role guides into
    # <project>/.pollypm/project-guides/ so every agent in this
    # project has an in-project absolute path to its role guide.
    # Idempotent: skips files that already exist (respecting any
    # forks from `pm project init-guide`).
    try:
        from pollypm.project_paths import materialize_role_guides
        materialize_role_guides(project_path)
    except Exception:  # noqa: BLE001
        # Materialization is a convenience — never block scaffold on it.
        pass
    return pollypm_dir


def scaffold_issue_tracker(project_path: Path) -> Path:
    project_root = normalize_project_path(project_path)
    ensure_project_scaffold(project_root)
    backend = get_task_backend(project_root)
    issues_dir = backend.ensure_tracker()
    validate = getattr(backend, "validate", None)
    if callable(validate):
        result = validate()
        if not getattr(result, "passed", False):
            errors = ", ".join(getattr(result, "errors", [])) or "unknown validation failure"
            raise RuntimeError(f"Task backend validation failed: {errors}")
    if isinstance(backend, FileTaskBackend):
        instructions_target = issues_dir / "instructions.md"
        if TRACKER_TEMPLATE.exists() and not instructions_target.exists():
            shutil.copyfile(TRACKER_TEMPLATE, instructions_target)
        _ensure_gitignore_entry(project_root, "issues/")
    return issues_dir


def enable_tracked_project(config_path: Path, project_key: str) -> KnownProject:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")
    scaffold_issue_tracker(project.path)
    project.kind = detect_project_kind(project.path)
    project.tracked = True
    config.projects[project_key] = project
    write_config(config, config_path, force=True)
    return project


def make_project_key(path: Path, existing: set[str]) -> str:
    base = slugify_project_key(path.name)
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def discover_git_repositories(
    scan_root: Path,
    *,
    known_paths: set[Path] | None = None,
) -> list[Path]:
    root = normalize_project_path(scan_root)
    known = {normalize_project_path(path) for path in known_paths or set()}
    found: list[Path] = []

    for current, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [
            name for name in dirnames
            if not _should_skip_dir(current_path, name)
        ]
        if ".git" in dirnames:
            repo_path = current_path.resolve()
            dirnames[:] = []
            if repo_path not in known:
                found.append(repo_path)

    return sorted(found)


def repository_last_commit_at(repo_path: Path) -> datetime | None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "-1", "--format=%ct"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return datetime.fromtimestamp(int(output), tz=UTC)
    except ValueError:
        return None


def _git_config_value(repo_path: Path, key: str) -> str | None:
    for command in (
        ["git", "-C", str(repo_path), "config", "--get", key],
        ["git", "config", "--global", "--get", key],
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return value
    return None


def repository_local_identity_candidates(repo_path: Path) -> list[str]:
    values: list[str] = []
    for key in ("user.email", "user.name"):
        value = _git_config_value(repo_path, key)
        if value:
            values.append(value)
    try:
        full_name = pwd.getpwuid(os.getuid()).pw_gecos.split(",", 1)[0].strip()
        if full_name:
            values.append(full_name)
    except KeyError:
        pass
    login = os.environ.get("USER") or os.environ.get("LOGNAME")
    if login:
        values.append(login)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
    return deduped


def repository_last_local_commit_at(repo_path: Path) -> datetime | None:
    latest: datetime | None = None
    for identity in repository_local_identity_candidates(repo_path):
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", "-1", f"--author={identity}", "--format=%ct"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        output = result.stdout.strip()
        if not output:
            continue
        try:
            commit_at = datetime.fromtimestamp(int(output), tz=UTC)
        except ValueError:
            continue
        if latest is None or commit_at > latest:
            latest = commit_at
    return latest


def discover_recent_git_repositories(
    scan_root: Path,
    *,
    known_paths: set[Path] | None = None,
    recent_days: int = 14,
) -> list[Path]:
    cutoff = datetime.now(UTC) - timedelta(days=recent_days)
    candidates = discover_git_repositories(scan_root, known_paths=known_paths)
    found: list[Path] = []
    for repo_path in candidates:
        last_commit = repository_last_local_commit_at(repo_path)
        if last_commit is None or last_commit < cutoff:
            continue
        found.append(repo_path)
    return found


def _should_skip_dir(parent: Path, name: str) -> bool:
    if name == ".git":
        return False
    if name in SKIP_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    candidate = parent / name
    return candidate.is_symlink()


def register_project(
    config_path: Path,
    repo_path: Path,
    *,
    name: str | None = None,
    slug: str | None = None,
) -> KnownProject:
    """Register a project under ``config_path``.

    ``slug`` lets callers override the auto-computed project key. The
    supplied slug is validated (must be canonical per
    :func:`slugify_project_key`) and checked for uniqueness against
    existing projects. When not provided, the key is computed from the
    repo path's last segment — the legacy behavior. See #766.
    """
    config = load_config(config_path)
    normalized_path = normalize_project_path(repo_path)
    if not normalized_path.exists() or not normalized_path.is_dir():
        raise typer.BadParameter(f"{normalized_path} is not a directory.")
    for project in config.projects.values():
        if normalize_project_path(project.path) == normalized_path:
            return project

    if slug is None:
        key = make_project_key(normalized_path, set(config.projects))
    else:
        key = slug.strip()
        canonical = slugify_project_key(key)
        if not canonical:
            raise typer.BadParameter(
                f"Slug {slug!r} does not yield a valid key "
                "(at least one alphanumeric character required)."
            )
        if canonical != key:
            raise typer.BadParameter(
                f"Slug {slug!r} is not in canonical form. "
                f"Try {canonical!r}."
            )
        if key in config.projects:
            raise typer.BadParameter(
                f"A project already exists at slug {key!r}. "
                "Pick a different slug or remove the conflict first."
            )
    project = KnownProject(
        key=key,
        path=normalized_path,
        name=name or normalized_path.name,
        persona_name=default_persona_name(name or normalized_path.name),
        kind=detect_project_kind(normalized_path),
    )
    config.projects[key] = project
    write_config(config, config_path, force=True)
    ensure_project_scaffold(normalized_path)
    return project


def rename_project(
    config_path: Path,
    old_slug: str,
    new_slug: str,
    *,
    dry_run: bool = False,
) -> tuple[KnownProject, list[str]]:
    """Rename a project's slug (key), updating config references.

    Returns ``(project, warnings)``. The project is the **new** entry
    (or the existing-old entry when ``dry_run=True``) and ``warnings``
    lists state that was NOT auto-updated and may need manual cleanup
    (tmux windows, worktree paths, work-service task IDs).

    Scope:
    - Moves the ``[projects.<old_slug>]`` config block to the new key,
      preserving all fields (path, name, persona_name, kind, tracked,
      role_assignments).
    - Updates every ``[sessions.*]`` entry whose ``project`` field
      equals ``old_slug`` to reference ``new_slug``.
    - Does NOT rename tmux windows (``architect-<slug>``,
      ``worker-<slug>``), worktree paths, or work-service task IDs
      (``<slug>/1`` etc.) — those are live state that requires session
      restart to pick up. The returned warning list names them so the
      caller can decide.

    ``dry_run=True`` reports what would change without mutating config.
    """
    normalized_new = slugify_project_key(new_slug)
    if not normalized_new:
        raise typer.BadParameter(
            f"New slug {new_slug!r} does not yield a valid key "
            f"(slugs must contain at least one alphanumeric character)."
        )
    if normalized_new != new_slug:
        raise typer.BadParameter(
            f"New slug {new_slug!r} is not in canonical form. "
            f"Try {normalized_new!r}."
        )

    config = load_config(config_path)
    if old_slug not in config.projects:
        raise typer.BadParameter(f"Unknown project: {old_slug}")
    if old_slug == new_slug:
        raise typer.BadParameter("New slug must differ from the old slug.")
    if new_slug in config.projects:
        raise typer.BadParameter(
            f"A project already exists at key {new_slug!r}. "
            "Pick a different slug or remove the conflict first."
        )

    warnings: list[str] = []
    project = config.projects[old_slug]
    renamed_project = KnownProject(
        key=new_slug,
        path=project.path,
        name=project.name,
        persona_name=project.persona_name,
        kind=project.kind,
        tracked=project.tracked,
        role_assignments=dict(project.role_assignments),
    )

    session_updates: list[tuple[str, str, str]] = []  # (name, old_project, new_project)
    for session_name, session in config.sessions.items():
        if session.project == old_slug:
            session_updates.append((session_name, old_slug, new_slug))

    # Flag live state the rename doesn't touch.
    for session_name, _, _ in session_updates:
        if session_name.endswith(f"_{old_slug}") or session_name.endswith(old_slug):
            warnings.append(
                f"Session name {session_name!r} still contains the old "
                f"slug; restart the session to pick up the new name."
            )
    tmux_windows_mentioning_old = [
        s.window_name for s in config.sessions.values()
        if s.window_name and old_slug in s.window_name
    ]
    if tmux_windows_mentioning_old:
        warnings.append(
            f"Tmux window names still reference {old_slug!r}: "
            f"{', '.join(sorted(set(tmux_windows_mentioning_old)))}. "
            "Kill + relaunch each affected session to pick up new names."
        )
    work_db = project.path / ".pollypm" / "state.db"
    if work_db.exists():
        warnings.append(
            f"Work-service task IDs in {work_db} still use the old "
            f"slug (e.g. {old_slug}/1). Existing tasks keep their IDs; "
            "new tasks will use the new slug."
        )
    worktree_root = project.path / ".pollypm" / "worktrees"
    if worktree_root.exists():
        old_worktrees = [p.name for p in worktree_root.iterdir() if old_slug in p.name]
        if old_worktrees:
            warnings.append(
                f"Worktree directories under {worktree_root} still use "
                f"the old slug: {', '.join(sorted(old_worktrees))}. "
                "Safe to leave; new worktrees will use the new slug."
            )

    if dry_run:
        return project, warnings

    # Apply mutations.
    del config.projects[old_slug]
    config.projects[new_slug] = renamed_project
    for session_name, _old, new in session_updates:
        session = config.sessions[session_name]
        config.sessions[session_name] = replace(session, project=new)
    write_config(config, config_path, force=True)
    return renamed_project, warnings


def remove_project(config_path: Path, project_key: str) -> KnownProject:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")
    session_refs = [
        session.name
        for session in config.sessions.values()
        if session.project == project_key and session.enabled
    ]
    if session_refs:
        raise typer.BadParameter(
            f"Project {project_key} is still used by sessions: {', '.join(session_refs)}"
        )
    del config.projects[project_key]
    write_config(config, config_path, force=True)
    return project


def scan_projects(
    config_path: Path,
    *,
    scan_root: Path | None = None,
    interactive: bool = True,
) -> list[KnownProject]:
    config = load_config(config_path)
    root = normalize_project_path(scan_root or DEFAULT_SCAN_ROOT)
    known_paths = {normalize_project_path(project.path) for project in config.projects.values()}
    discovered = discover_recent_git_repositories(root, known_paths=known_paths, recent_days=14)
    added: list[KnownProject] = []

    for repo_path in discovered:
        default_choice = _default_add_choice(repo_path, config.project.workspace_root)
        if interactive:
            add = typer.confirm(
                f"Add project {repo_path.name} at {repo_path}?",
                default=default_choice,
            )
            if not add:
                continue
        project = KnownProject(
            key=make_project_key(repo_path, set(config.projects) | {item.key for item in added}),
            path=repo_path,
            name=repo_path.name,
            persona_name=default_persona_name(repo_path.name),
            kind=ProjectKind.GIT,
        )
        config.projects[project.key] = project
        ensure_project_scaffold(repo_path)
        added.append(project)

    if added:
        write_config(config, config_path, force=True)

    return added


def set_workspace_root(config_path: Path, workspace_root: Path) -> Path:
    config = load_config(config_path)
    config.project.workspace_root = normalize_project_path(workspace_root)
    write_config(config, config_path, force=True)
    return config.project.workspace_root


def _default_add_choice(repo_path: Path, workspace_root: Path) -> bool:
    try:
        repo_path.resolve().relative_to(workspace_root.expanduser().resolve())
        return True
    except ValueError:
        return False


def _ensure_gitignore_entry(project_root: Path, entry: str) -> None:
    gitignore_path = normalize_project_path(project_root) / ".gitignore"
    existing = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    if entry in existing:
        return
    if existing and existing[-1] != "":
        existing.append("")
    existing.append(entry)
    gitignore_path.write_text("\n".join(existing).rstrip() + "\n")
