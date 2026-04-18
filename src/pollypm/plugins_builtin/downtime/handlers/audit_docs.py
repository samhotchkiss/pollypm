"""``audit_docs`` exploration handler.

Reviews code vs docs and produces a draft PR with targeted doc updates.

dt05 scope: set up the doc-drift worktree skeleton — branch name,
artifact path, `gh pr create --draft` invocation contract. The actual
doc-edits come from the explorer's session. The handler surfaces the
structured done-output the dt06 apply handler routes on.

Note: we do not call ``gh`` from this handler at dt05 because pm ui /
pm attach is forbidden in tests. We return the inputs the apply step
uses (branch name, title, body) so dt06 can invoke gh from the apply
path and we can mock it cleanly.
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
class AuditDocsResult:
    """Structured output of an audit_docs exploration."""

    branch_name: str
    pr_title: str
    pr_body: str
    summary: str
    slug: str
    # The PR isn't opened until dt06's apply path runs (which is after
    # human approval). dt05 returns 0/empty as placeholders.
    pr_number: int = 0
    pr_url: str = ""


_AUDIT_SCRATCH_REL = Path(".pollypm") / "downtime-audits"


def render_audit_report(*, title: str, description: str) -> str:
    return (
        f"# Doc audit: {title.strip()}\n"
        "\n"
        "> Produced by the downtime explorer. Each section below is a "
        "doc-drift observation the explorer noticed while scanning "
        "recent code changes against shipped docs.\n"
        "\n"
        "## Candidate scope\n"
        "\n"
        f"{description.strip() or '(no description provided)'}\n"
        "\n"
        "## Observations\n"
        "\n"
        "_Explorer: list the (code → doc) mismatches you found. One "
        "bullet per observation with a suggested fix._\n"
        "\n"
        "## Suggested doc updates\n"
        "\n"
        "_Explorer: quote-blocks of the exact patches you want to "
        "apply. dt06 opens these as a draft PR on approval._\n"
    )


def run_audit_docs(
    *,
    project_root: Path,
    title: str,
    description: str,
) -> AuditDocsResult:
    """Set up the doc-audit branch + scratch report.

    Does **not** open the PR — that's the apply handler's job (dt06).
    Returns the pr_title / pr_body contents the apply step will use.
    """
    slug = slugify(title)
    branch_name = f"downtime/docs-{slug}"
    scratch_rel = _AUDIT_SCRATCH_REL / f"{slug}.md"
    scratch_path = project_root / scratch_rel
    scratch_path.parent.mkdir(parents=True, exist_ok=True)
    scratch_path.write_text(render_audit_report(title=title, description=description))

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
                f"downtime(docs): scaffold audit branch for {title}",
                "--allow-empty",
            ],
            cwd=project_root,
        )
        if saved and saved != branch_name:
            _run_git(["switch", saved], cwd=project_root)

    pr_title = f"downtime/docs: {title.strip()}"
    pr_body = (
        "Draft PR produced by the downtime doc-audit explorer. Awaiting "
        "human review.\n\n"
        f"Candidate scope: {description.strip() or '(no description)'}\n\n"
        "See the scratch report under "
        f"`{scratch_rel}` for the per-observation suggested edits."
    )
    summary = (
        f"Queued a draft doc-audit PR on branch '{branch_name}' for "
        f"'{title.strip()}'. The PR itself opens when the human approves "
        "the downtime task."
    )
    return AuditDocsResult(
        branch_name=branch_name,
        pr_title=pr_title,
        pr_body=pr_body,
        summary=summary,
        slug=slug,
    )
