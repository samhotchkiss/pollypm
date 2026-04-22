**Last Verified:** 2026-04-22

## Summary

A **project** is a known local repo or folder registered in `pollypm.toml` under `[projects.<key>]` with a path, name, and kind (`git` / `folder`). `pollypm.projects` owns registration, scaffolding (creating `docs/`, `.pollypm/`, and `issues/` directories), session locks, and persona-name assignment.

**Worktrees** are per-session isolated git checkouts under `<project>/.pollypm/worktrees/<session>/`. They are the safety boundary for parallel agents: one worker can ship a feature while another fixes tests without trampling the same branch. `pollypm.worktrees` owns creation (`ensure_worktree`), listing, audit, and the shared worktree ledger in `StateStore.worktrees`.

Touch this module when changing worktree layout, adding a new project kind, or changing the scaffold set. Do not parse TOML directly — route through `pollypm.config`.

## Core Contracts

```python
# src/pollypm/projects.py
DEFAULT_WORKSPACE_ROOT = Path.home() / "dev"

def register_project(config_path: Path, project_path: Path) -> tuple[str, str]: ...
def remove_project(config_path: Path, key: str) -> tuple[str, str]: ...
def enable_tracked_project(config_path: Path, key: str) -> tuple[str, bool]: ...
def set_workspace_root(config_path: Path, path: Path) -> Path: ...

def ensure_project_scaffold(project_path: Path) -> None: ...
def project_checkpoints_dir(project_path: Path) -> Path: ...
def project_transcripts_dir(project_path: Path) -> Path: ...
def project_worktrees_dir(project_path: Path) -> Path: ...
def session_scoped_dir(base: Path, session_id: str) -> Path: ...

def ensure_session_lock(path: Path, session_id: str) -> None: ...
def release_session_lock(path: Path) -> None: ...

# src/pollypm/worktrees.py
_SAFE_WORKTREE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

def ensure_worktree(
    config_path: Path,
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    session_name: str | None = None,
    issue_key: str | None = None,
) -> WorktreeRecord | None: ...

def list_worktrees(config_path: Path, *, project_key: str | None = None) -> list[WorktreeRecord]: ...
```

## File Structure

- `src/pollypm/projects.py` — registration, scaffolding, locks, persona naming.
- `src/pollypm/worktrees.py` — `ensure_worktree`, worktree ledger.
- `src/pollypm/worktree_audit.py` — reconciliation between git's worktree registry and the ledger.
- `src/pollypm/project_intelligence.py` — project-dashboard data aggregation.
- `src/pollypm/doc_scaffold.py` — `scaffold_docs` bootstraps the `docs/` set when a project is first registered.
- `src/pollypm/defaults/docs/` — doc templates (`project-overview.md`, `decisions.md`, etc.).
- `src/pollypm/storage/state.py` — owns the `worktrees` table (`WorktreeRecord`).
- `src/pollypm/plugins_builtin/core_recurring/plugin.py` — schedules `worktree.state_audit` (`@every 10m`) and `agent_worktree.prune` (hourly).

## Implementation Details

- **Scaffold.** `ensure_project_scaffold(path)` creates `<project>/.pollypm/` with `config/`, `transcripts/`, `artifacts/checkpoints/`, `worktrees/`, `logs/`, and `inbox/`. It also stamps a `docs/` set (via `doc_scaffold.scaffold_docs`) unless the user declined during onboarding.
- **Worktree key validation.** `_SAFE_WORKTREE_KEY_RE` (`^[a-zA-Z0-9_-]+$`) is enforced on `project_key`, `lane_kind`, and `lane_key` before they reach any git command — these values become branch / path components.
- **Worktree path.** `<project>/.pollypm/worktrees/<project_key>-<lane_kind>-<lane_key>/` by default. `session_scoped_dir` is the single source of truth for the path template.
- **Session lock.** `ensure_session_lock` writes a lockfile with the session id so two sessions cannot claim the same worktree. `release_session_lock` removes it on teardown.
- **Folder projects.** Projects with `kind = "folder"` skip git operations. `ensure_worktree` returns `None` when `project.path` has no `.git` directory — callers handle this as "no worktree, use the project root."
- **Audit.** `worktree.state_audit` (every 10 min) reconciles `git worktree list --porcelain` with the ledger. `agent_worktree.prune` (hourly) removes orphan worktrees whose session ended >24h ago.

## Related Docs

- [modules/work-service.md](work-service.md) — per-task `SessionManager` calls `ensure_worktree` on task claim.
- [modules/config.md](config.md) — `[projects.<key>]` entries and `workspace_root`.
- [features/service-api.md](../features/service-api.md) — project-management surface.
