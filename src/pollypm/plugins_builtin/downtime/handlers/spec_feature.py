"""``spec_feature`` exploration handler.

Writes a draft markdown spec to ``docs/ideas/<slug>.md``. The
explorer persona drafts the actual content during a session; this
handler provides the pure-Python skeleton that:

1. Computes the artifact path.
2. Renders a draft stub from the candidate's title/description, so the
   explorer always has a seed file to edit.
3. Commits the stub on a ``downtime/spec-<slug>`` branch (when run
   inside a git worktree — outside git we still produce the file).

The dt06 ``apply`` handler moves the draft to ``docs/specs/<slug>.md``
on approval or archives it on rejection.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


SPEC_DRAFT_DIR = Path("docs") / "ideas"
SPEC_COMMITTED_DIR = Path("docs") / "specs"


@dataclass(slots=True, frozen=True)
class SpecFeatureResult:
    """Structured output of a spec_feature exploration."""

    artifact_path: str
    branch_name: str
    summary: str
    slug: str


def slugify(title: str) -> str:
    """Lowercase, hyphenated slug safe for filenames and git branches."""
    lowered = title.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = lowered.strip("-")
    return lowered or "untitled"


def render_spec_stub(
    *, title: str, description: str, source: str = "downtime"
) -> str:
    """Seed content for a fresh spec draft.

    Downstream the explorer edits this file to flesh out the real
    spec. Including the source + original description keeps the
    provenance trail intact when a human later reviews the inbox entry.
    """
    return (
        f"# {title.strip()}\n"
        "\n"
        f"> Draft spec — produced by the downtime explorer ({source}).\n"
        f"> Status: **draft / awaiting human review**.\n"
        "\n"
        "## Candidate description\n"
        "\n"
        f"{description.strip() or '(no description provided)'}\n"
        "\n"
        "## Motivation\n"
        "\n"
        "_Explorer: fill this in with why this feature matters._\n"
        "\n"
        "## Proposed design\n"
        "\n"
        "_Explorer: outline the design. Keep it scoped — one-session's "
        "worth of detail, enough for a reviewer to accept or reject._\n"
        "\n"
        "## Open questions\n"
        "\n"
        "_Explorer: list anything you couldn't resolve in one pass._\n"
    )


def _run_git(args: list[str], *, cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, "git binary not found"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _current_branch(cwd: Path) -> str | None:
    code, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if code != 0:
        return None
    return out.strip() or None


def _is_git_repo(cwd: Path) -> bool:
    code, _ = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return code == 0


def run_spec_feature(
    *,
    project_root: Path,
    title: str,
    description: str,
    source: str = "downtime",
) -> SpecFeatureResult:
    """Produce a draft spec + commit on a downtime branch.

    Returns the structured result the apply handler routes on. The
    function is deliberately idempotent-ish: if the target file exists
    we overwrite it (the explorer may re-run for refinement) but we
    never delete or merge elsewhere.

    Side effects:

    * Writes ``docs/ideas/<slug>.md`` under ``project_root``.
    * If the project is a git working tree, creates (or switches to)
      branch ``downtime/spec-<slug>`` and commits the draft.
    """
    slug = slugify(title)
    artifact_rel = SPEC_DRAFT_DIR / f"{slug}.md"
    artifact_path = project_root / artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(render_spec_stub(title=title, description=description, source=source))

    branch_name = f"downtime/spec-{slug}"
    if _is_git_repo(project_root):
        saved = _current_branch(project_root)
        # Create branch if missing, else switch to it. `-c` creates
        # only if it doesn't exist; `checkout -B` would reset an
        # existing branch which we don't want mid-explore.
        code, _ = _run_git(["switch", "-c", branch_name], cwd=project_root)
        if code != 0:
            # Already exists — try a plain switch.
            _run_git(["switch", branch_name], cwd=project_root)
        _run_git(["add", str(artifact_rel)], cwd=project_root)
        _run_git(
            [
                "commit",
                "-m",
                f"downtime(spec): draft spec for {title}",
                "--allow-empty",
            ],
            cwd=project_root,
        )
        if saved and saved != branch_name:
            # Leave the user's working branch checked out — the draft
            # lives on the downtime branch. The file on disk still
            # reflects the committed draft until the user switches.
            _run_git(["switch", saved], cwd=project_root)

    summary = (
        f"Drafted a spec stub for '{title.strip()}' at {artifact_rel} "
        f"on branch {branch_name}. Awaiting human review."
    )
    return SpecFeatureResult(
        artifact_path=str(artifact_rel),
        branch_name=branch_name,
        summary=summary,
        slug=slug,
    )
