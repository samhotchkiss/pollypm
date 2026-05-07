from __future__ import annotations

import difflib
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pollypm.projects import project_pollypm_dir

PROJECT_GUIDES_DIRNAME = "project-guides"
SUPPORTED_PROJECT_GUIDE_ROLES: tuple[str, ...] = (
    "architect",
    "reviewer",
    "worker",
)


@dataclass(frozen=True, slots=True)
class ProjectGuideInfo:
    role: str
    path: Path
    forked_from: str | None
    body: str


@dataclass(frozen=True, slots=True)
class ProjectGuideDriftInfo:
    role: str
    path: Path
    forked_from: str | None
    current_ref: str
    drifted: bool
    body: str
    upstream_body: str


def normalize_project_guide_role(role: str) -> str:
    return role.strip().lower().replace("-", "_")


def validate_project_guide_role(role: str) -> str:
    normalized = normalize_project_guide_role(role)
    if normalized == "operator_pm":
        raise ValueError(
            "Project-local guides do not support operator_pm. "
            "Polly always uses the built-in operator guide."
        )
    if normalized not in SUPPORTED_PROJECT_GUIDE_ROLES:
        available = ", ".join(SUPPORTED_PROJECT_GUIDE_ROLES)
        raise ValueError(
            f"Unsupported role '{role}'. Available: {available}."
        )
    return normalized


def project_guides_dir(project_path: Path) -> Path:
    return project_pollypm_dir(project_path) / PROJECT_GUIDES_DIRNAME


def project_guide_path(project_path: Path, role: str) -> Path:
    normalized = validate_project_guide_role(role)
    return project_guides_dir(project_path) / f"{normalized}.md"


def init_project_guide(
    project_path: Path,
    role: str,
    *,
    force: bool = False,
) -> ProjectGuideInfo:
    normalized = validate_project_guide_role(role)
    built_in = built_in_guide_text(normalized)
    built_in_source = built_in_guide_source_path(normalized)
    forked_from = built_in_guide_fork_ref(
        normalized,
        content=built_in,
        source_path=built_in_source,
    )
    target = project_guide_path(project_path, normalized)
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} already exists. Re-run with --force to overwrite."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _render_front_matter(forked_from) + built_in.rstrip() + "\n",
        encoding="utf-8",
    )
    return read_project_guide(project_path, normalized)


def list_project_guides(project_path: Path) -> list[ProjectGuideInfo]:
    guide_dir = project_guides_dir(project_path)
    if not guide_dir.exists():
        return []
    guides: list[ProjectGuideInfo] = []
    for path in sorted(guide_dir.glob("*.md")):
        guides.append(_read_project_guide_path(path))
    return guides


def project_guide_drift_info(
    project_path: Path,
    role: str,
) -> ProjectGuideDriftInfo | None:
    normalized = validate_project_guide_role(role)
    target = project_guide_path(project_path, normalized)
    if not target.exists():
        return None
    guide = _read_project_guide_path(target)
    upstream_body = built_in_guide_text(normalized).strip()
    current_ref = built_in_guide_fork_ref(
        normalized,
        content=upstream_body,
        source_path=built_in_guide_source_path(normalized),
    )
    return ProjectGuideDriftInfo(
        role=guide.role,
        path=guide.path,
        forked_from=guide.forked_from,
        current_ref=current_ref,
        drifted=(guide.forked_from != current_ref),
        body=guide.body,
        upstream_body=upstream_body,
    )


def list_drifted_project_guides(project_path: Path) -> list[ProjectGuideDriftInfo]:
    drifted: list[ProjectGuideDriftInfo] = []
    for guide in list_project_guides(project_path):
        info = project_guide_drift_info(project_path, guide.role)
        if info is not None and info.drifted:
            drifted.append(info)
    return drifted


def render_project_guide_diff(project_path: Path, role: str) -> str:
    info = project_guide_drift_info(project_path, role)
    if info is None:
        raise FileNotFoundError(
            f"No project-local {validate_project_guide_role(role)} guide exists."
        )
    from_label = f"project-local/{info.role}.md (forked_from {info.forked_from or 'unknown'})"
    to_label = f"built-in/{info.role}.md (current {info.current_ref})"
    diff = difflib.unified_diff(
        info.body.splitlines(),
        info.upstream_body.splitlines(),
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
    )
    return "\n".join(diff)


def read_project_guide(project_path: Path, role: str) -> ProjectGuideInfo:
    normalized = validate_project_guide_role(role)
    return _read_project_guide_path(project_guide_path(project_path, normalized))


def resolve_project_guide_text(
    project_path: Path,
    role: str,
    *,
    fallback_text: str | None = None,
) -> str:
    normalized = validate_project_guide_role(role)
    target = project_guides_dir(project_path) / f"{normalized}.md"
    if target.exists():
        return _read_project_guide_path(target).body
    if fallback_text is not None:
        return fallback_text.strip()
    return built_in_guide_text(normalized).strip()


def worker_guide_reference(project_path: Path) -> str:
    target = project_guides_dir(project_path) / "worker.md"
    if target.exists():
        return str(target.resolve())
    return "docs/worker-guide.md"


def built_in_guide_text(role: str) -> str:
    normalized = validate_project_guide_role(role)
    if normalized == "architect":
        path = built_in_guide_source_path(normalized)
        if path is None:
            raise RuntimeError("Could not locate the built-in architect guide.")
        return path.read_text(encoding="utf-8")
    if normalized == "reviewer":
        from pollypm.plugins_builtin.core_agent_profiles.profiles import reviewer_prompt

        return reviewer_prompt()
    if normalized == "worker":
        path = built_in_guide_source_path(normalized)
        if path is None:
            raise RuntimeError("Could not locate docs/worker-guide.md.")
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Unsupported role '{role}'.")


def built_in_guide_source_path(role: str) -> Path | None:
    normalized = validate_project_guide_role(role)
    if normalized == "architect":
        return Path(__file__).resolve().parent / "plugins_builtin" / "project_planning" / "profiles" / "architect.md"
    if normalized == "reviewer":
        return Path(__file__).resolve().parent / "plugins_builtin" / "core_agent_profiles" / "profiles.py"
    if normalized == "worker":
        # Prefer the repo copy (editable install / in-tree dev) so
        # forks of the doc surface during dev cycles. In a packaged
        # install the repo copy isn't present — fall back to the
        # bundled copy under ``pollypm/defaults/docs/worker-guide.md``
        # so workers in any worktree get a real guide path.
        repo_copy = _locate_repo_file("docs/worker-guide.md")
        if repo_copy is not None:
            return repo_copy
        packaged = (
            Path(__file__).resolve().parent
            / "defaults"
            / "docs"
            / "worker-guide.md"
        )
        return packaged if packaged.is_file() else None
    raise ValueError(f"Unsupported role '{role}'.")


def built_in_guide_fork_ref(
    role: str,
    *,
    content: str | None = None,
    source_path: Path | None = None,
) -> str:
    normalized = validate_project_guide_role(role)
    guide_content = content if content is not None else built_in_guide_text(normalized)
    guide_source = source_path if source_path is not None else built_in_guide_source_path(normalized)
    git_sha = _git_sha_for_path(guide_source)
    if git_sha:
        return git_sha
    digest = hashlib.sha256(guide_content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _locate_repo_file(relative_path: str) -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / relative_path
        if candidate.is_file():
            return candidate
    return None


def _git_sha_for_path(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        repo = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if repo.returncode != 0:
        return None
    repo_root = Path(repo.stdout.strip())
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-n", "1", "--format=%H", "--", str(relative)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _read_project_guide_path(path: Path) -> ProjectGuideInfo:
    text = path.read_text(encoding="utf-8")
    header, body = _split_front_matter(text)
    return ProjectGuideInfo(
        role=path.stem,
        path=path,
        forked_from=header.get("forked_from"),
        body=body.strip(),
    )


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        header: dict[str, str] = {}
        for raw_line in lines[1:index]:
            if ":" not in raw_line:
                continue
            key, value = raw_line.split(":", 1)
            header[key.strip()] = value.strip()
        body = "\n".join(lines[index + 1:])
        return header, body
    return {}, text


def _render_front_matter(forked_from: str) -> str:
    return f"---\nforked_from: {forked_from}\n---\n\n"
