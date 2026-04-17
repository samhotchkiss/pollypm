"""``try_alt_approach`` exploration handler.

Implements an alternative to an existing approach on a downtime branch
and writes a comparison report at ``docs/comparisons/<slug>.md``.

Like the other branch-producing handlers, dt05 only scaffolds the
workspace (branch + report stub). The explorer writes the real
implementation + comparison during the session; dt06's apply path
decides whether to keep the branch or archive it.
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


COMPARISON_DIR = Path("docs") / "comparisons"


@dataclass(slots=True, frozen=True)
class TryAltApproachResult:
    """Structured output of a try_alt_approach exploration."""

    branch_name: str
    comparison_path: str
    summary: str
    verdict: str
    slug: str


VALID_VERDICTS: tuple[str, ...] = ("better", "worse", "equivalent", "unresolved")


def render_comparison_stub(*, title: str, description: str) -> str:
    return (
        f"# Alternative approach: {title.strip()}\n"
        "\n"
        "> Produced by the downtime explorer (try_alt_approach). The "
        "alternative implementation lives on the downtime branch linked "
        "above; this document is the side-by-side comparison the human "
        "reviewer reads before deciding whether to adopt it.\n"
        "\n"
        "## Candidate description\n"
        "\n"
        f"{description.strip() or '(no description provided)'}\n"
        "\n"
        "## Current approach (summary)\n"
        "\n"
        "_Explorer: describe how the code does it today._\n"
        "\n"
        "## Alternative approach (summary)\n"
        "\n"
        "_Explorer: describe the alternative._\n"
        "\n"
        "## Tradeoffs\n"
        "\n"
        "_Explorer: table form is fine. Dimensions: correctness, "
        "complexity, performance, maintainability, testability._\n"
        "\n"
        "## Verdict\n"
        "\n"
        "_Explorer: one of `better` / `worse` / `equivalent` / "
        "`unresolved`. Include a one-line rationale. Ambiguity is fine "
        "— 'unresolved' is a valid exploration outcome._\n"
    )


def run_try_alt_approach(
    *,
    project_root: Path,
    title: str,
    description: str,
) -> TryAltApproachResult:
    """Set up the alt-approach branch + comparison report stub."""
    slug = slugify(title)
    branch_name = f"downtime/{slug}"
    report_rel = COMPARISON_DIR / f"{slug}.md"
    report_path = project_root / report_rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_comparison_stub(title=title, description=description))

    if _is_git_repo(project_root):
        saved = _current_branch(project_root)
        code, _ = _run_git(["switch", "-c", branch_name], cwd=project_root)
        if code != 0:
            _run_git(["switch", branch_name], cwd=project_root)
        _run_git(["add", str(report_rel)], cwd=project_root)
        _run_git(
            [
                "commit",
                "-m",
                f"downtime(alt): scaffold alt-approach branch for {title}",
                "--allow-empty",
            ],
            cwd=project_root,
        )
        if saved and saved != branch_name:
            _run_git(["switch", saved], cwd=project_root)

    summary = (
        f"Scaffolded an alt-approach branch '{branch_name}' and comparison "
        f"stub at {report_rel}. Awaiting explorer session + human review."
    )
    return TryAltApproachResult(
        branch_name=branch_name,
        comparison_path=str(report_rel),
        summary=summary,
        verdict="unresolved",
        slug=slug,
    )
