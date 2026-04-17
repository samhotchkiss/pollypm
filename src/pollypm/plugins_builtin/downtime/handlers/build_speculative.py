"""``build_speculative`` exploration handler.

Creates a ``downtime/<slug>`` branch for a speculative prototype. The
explorer persona does the actual coding; the handler provides the
branch-setup scaffold and the structured done-output shape.

dt05 scope:
* compute slug + branch name
* create branch in-place (or return a deterministic name if not in git)
* produce a stub scratch file so the branch has a commit

dt06's apply handler opens a PR from this branch on approval.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.downtime.handlers.spec_feature import (
    _current_branch,
    _is_git_repo,
    _run_git,
    slugify,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class BuildSpeculativeResult:
    """Structured output of a build_speculative exploration."""

    branch_name: str
    commit_sha: str
    summary: str
    tests_added: int
    tests_pass: bool
    slug: str


_SCRATCH_REL = Path(".pollypm-state") / "downtime-scratch"


def render_scratch_readme(*, title: str, description: str) -> str:
    """Seed content for the scratch file pushed to the downtime branch."""
    return (
        f"# Speculative prototype: {title.strip()}\n"
        "\n"
        "> Work-in-progress. This branch is a downtime-plugin exploration "
        "and is **not** merged into main. The human reviewer approves or "
        "archives it per the usual downtime flow (see spec §6).\n"
        "\n"
        "## Candidate description\n"
        "\n"
        f"{description.strip() or '(no description provided)'}\n"
        "\n"
        "## What the explorer plans to build\n"
        "\n"
        "_Explorer: outline scope. Keep it completable in one session._\n"
        "\n"
        "## Tests added\n"
        "\n"
        "_Explorer: list any tests added and whether they pass._\n"
    )


def run_build_speculative(
    *,
    project_root: Path,
    title: str,
    description: str,
) -> BuildSpeculativeResult:
    """Set up the downtime branch + seed scratch file.

    Returns the structured result. The commit sha is the HEAD of the
    newly-created branch after the seed commit (or ``""`` when not in
    a git repo).
    """
    slug = slugify(title)
    branch_name = f"downtime/{slug}"
    scratch_rel = _SCRATCH_REL / f"{slug}.md"
    scratch_path = project_root / scratch_rel
    scratch_path.parent.mkdir(parents=True, exist_ok=True)
    scratch_path.write_text(render_scratch_readme(title=title, description=description))

    commit_sha = ""
    if _is_git_repo(project_root):
        saved = _current_branch(project_root)
        code, _ = _run_git(["switch", "-c", branch_name], cwd=project_root)
        if code != 0:
            _run_git(["switch", branch_name], cwd=project_root)
        _run_git(["add", str(scratch_rel)], cwd=project_root)
        _run_git(
            [
                "commit",
                "-m",
                f"downtime(build): scaffold speculative branch for {title}",
                "--allow-empty",
            ],
            cwd=project_root,
        )
        rc, out = _run_git(["rev-parse", "HEAD"], cwd=project_root)
        if rc == 0:
            commit_sha = out.strip()
        if saved and saved != branch_name:
            _run_git(["switch", saved], cwd=project_root)

    summary = (
        f"Scaffolded a speculative prototype branch '{branch_name}' for "
        f"'{title.strip()}'. Scratch notes at {scratch_rel}. Awaiting "
        "explorer session + human review."
    )
    return BuildSpeculativeResult(
        branch_name=branch_name,
        commit_sha=commit_sha,
        summary=summary,
        tests_added=0,
        tests_pass=True,  # nothing to run yet; explorer updates this
        slug=slug,
    )
