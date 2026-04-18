"""``security_scan`` exploration handler.

**Report-only.** Per spec §6, security_scan is the one downtime
category that deliberately never produces a branch or code changes.
Findings sit in a report; the user decides whether the fix itself is
a separate downtime task or a scheduled planning item.

Enforcement (spec §10):

* The handler writes exclusively under ``.pollypm/security-reports/``.
* :func:`validate_no_source_changes` checks that no tracked file
  outside the report dir was modified during the exploration. dt06's
  apply path calls this before stamping the report as reviewed — if
  it fails, the apply step refuses to proceed regardless of the
  approval decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.downtime.handlers.spec_feature import slugify


logger = logging.getLogger(__name__)


REPORT_DIR = Path(".pollypm") / "security-reports"
# Files under this prefix are the only ones the handler is allowed to
# write. Anything else means the explorer tried to write code — which
# is a hard refusal for this category.
ALLOWED_PREFIX = REPORT_DIR.as_posix()


@dataclass(slots=True, frozen=True)
class SecurityScanResult:
    """Structured output of a security_scan exploration."""

    report_path: str
    severity: str
    finding_count: int
    summary: str
    slug: str


_SEVERITY_LEVELS: tuple[str, ...] = ("info", "low", "medium", "high", "critical")


def render_report_stub(
    *, title: str, description: str, date_str: str
) -> str:
    return (
        f"# Security report: {title.strip()}\n"
        "\n"
        f"- Date: {date_str}\n"
        "- Produced by: downtime explorer (security_scan)\n"
        "- Status: **awaiting human review**\n"
        "- Severity: _explorer: set one of info / low / medium / high / critical_\n"
        "\n"
        "## Scope\n"
        "\n"
        f"{description.strip() or '(no scope description)'}\n"
        "\n"
        "## Findings\n"
        "\n"
        "_Explorer: enumerate findings. One section per finding; include "
        "evidence (file:line), attacker model, blast radius, and "
        "suggested remediation. No code changes — fixes go in a "
        "follow-up downtime task or a planning item._\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "_Explorer: state the overall recommendation (fix-now / fix-soon "
        "/ monitor / no-op) with a one-line justification._\n"
    )


def report_filename(*, title: str, today: _date | None = None) -> Path:
    """Deterministic report filename: ``<YYYY-MM-DD>-<slug>.md``."""
    stamp = (today or _date.today()).isoformat()
    slug = slugify(title)
    return REPORT_DIR / f"{stamp}-{slug}.md"


def run_security_scan(
    *,
    project_root: Path,
    title: str,
    description: str,
    today: _date | None = None,
) -> SecurityScanResult:
    """Produce the report stub. **No branch. No code changes.**

    The handler is intentionally narrow — the explorer writes findings
    into the report later. dt06's apply step stamps the report with
    reviewer + date (approval) or a dismissal line (rejection); it
    never creates commits or touches files outside REPORT_DIR.
    """
    rel = report_filename(title=title, today=today)
    path = project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    date_str = (today or _date.today()).isoformat()
    path.write_text(
        render_report_stub(title=title, description=description, date_str=date_str)
    )

    summary = (
        f"Produced a security-scan report stub at {rel}. No branch, no "
        f"code changes — findings live in the report only."
    )
    return SecurityScanResult(
        report_path=str(rel),
        severity="info",  # explorer refines during session
        finding_count=0,
        summary=summary,
        slug=slugify(title),
    )


def validate_no_source_changes(
    *, changed_paths: list[str | Path],
) -> tuple[bool, list[str]]:
    """Return (ok, offending_paths).

    ``ok`` is True only if every path in ``changed_paths`` sits under
    :data:`REPORT_DIR`. dt06's apply path calls this before stamping
    the report; any violation means the apply step refuses to proceed.
    The returned list enumerates every offending path for the
    rejection reason.
    """
    offenders: list[str] = []
    for raw in changed_paths:
        posix = Path(raw).as_posix()
        if not posix.startswith(ALLOWED_PREFIX + "/") and posix != ALLOWED_PREFIX:
            offenders.append(posix)
    return (not offenders), offenders
