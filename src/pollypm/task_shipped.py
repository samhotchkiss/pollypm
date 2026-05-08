"""Emit a 'task shipped' inbox card on pm task done.

When a worker completes a task via ``pm task done --output ...``, the work
output may include artifacts like commit SHAs and external action URLs
(deploy links). This module observes that transition and emits a single
inbox notification with the deliverable surface — URL prominent, commit
SHAs listed, summary quoted.

The aim is the "magic" side of pm: when work ships, the operator sees a
delivery card in their inbox without having to ask "is it done?". The
data is read from the worker's reported ``WorkOutput`` artifacts — no
plugin imports, no scraping. Workers that don't populate artifacts get
a plain summary card; workers that do populate get a richer one.

Boundary discipline: this module only imports from
:mod:`pollypm.work.models` (typed value objects). It does not import
from any plugin and does not reach into private store internals.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from pollypm.work.models import ArtifactKind

logger = logging.getLogger(__name__)

# URL pattern is conservative on purpose: we only surface http(s) links,
# and we trim a few common trailing punctuation characters that show up
# when URLs land at the end of free-form descriptions.
_URL_RE = re.compile(r"https?://[^\s\)<>]+")
_URL_TRAILING_TRIM = ".,;:!?)"

# Cap commit SHA display length. Full SHAs in a notification card are
# noise; the short form is enough to grep / `git show` without
# overwhelming the inbox subject.
_SHA_DISPLAY_LEN = 12

# Cap the title summary so it fits the inbox subject column. The full
# summary still appears in the body.
_TITLE_SUMMARY_LEN = 80

_TASK_SHIPPED_LABEL = "task_shipped"
_HAS_DELIVERABLE_URL_LABEL = "has_deliverable_url"


class _TaskShippedSvc(Protocol):
    """Minimal contract — what the emit hook needs from the work service.

    The transition hook in ``service_transitions.on_task_done`` already
    holds an ``SQLiteWorkService``; this protocol just documents the
    methods we touch so the function is independently mockable in
    tests.
    """

    def get(self, task_id: str) -> Any: ...
    def get_execution(self, task_id: str) -> list[Any]: ...
    def create(self, **kwargs: Any) -> Any: ...


def _latest_work_output(svc: _TaskShippedSvc, task_id: str) -> Any | None:
    """Return the work_output of the most recent execution, or None.

    Walks executions newest-first and returns the first one with a
    populated ``work_output``. Returns None when there are no
    executions, none have a work_output, or the lookup itself fails.
    """
    try:
        executions = svc.get_execution(task_id)
    except Exception:  # noqa: BLE001
        return None
    for execution in reversed(executions):
        wo = getattr(execution, "work_output", None)
        if wo is not None:
            return wo
    return None


def _trim_url(url: str) -> str:
    while url and url[-1] in _URL_TRAILING_TRIM:
        url = url[:-1]
    return url


def _extract_commit_refs(work_output: Any) -> list[str]:
    """Pull commit SHAs from artifacts of kind COMMIT, short-formatted."""
    refs: list[str] = []
    for art in getattr(work_output, "artifacts", None) or []:
        if getattr(art, "kind", None) != ArtifactKind.COMMIT:
            continue
        ref = getattr(art, "ref", None) or getattr(art, "external_ref", None)
        if not ref:
            continue
        refs.append(str(ref)[:_SHA_DISPLAY_LEN])
    return refs


def _extract_deploy_urls(work_output: Any) -> list[str]:
    """Pull http(s) URLs from artifacts of kind ACTION.

    URLs may sit in either ``external_ref`` (preferred) or be embedded
    in ``description``. We extract from both, deduping in order.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for art in getattr(work_output, "artifacts", None) or []:
        if getattr(art, "kind", None) != ArtifactKind.ACTION:
            continue
        ref = (getattr(art, "external_ref", None) or "").strip()
        if ref.startswith(("http://", "https://")):
            url = _trim_url(ref)
            if url not in seen:
                seen.add(url)
                urls.append(url)
            continue
        desc = getattr(art, "description", None) or ""
        for match in _URL_RE.findall(desc):
            url = _trim_url(match)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _build_card_title(task_id: str, summary: str, deploys: list[str]) -> str:
    """Compose the inbox subject line.

    Priority: shipped marker → deploy URL (if any, primary) → summary
    (if no deploy). The deploy URL is the most actionable signal so it
    earns the subject; commit SHAs are body-only.
    """
    parts = [f"✓ {task_id} SHIPPED"]
    if deploys:
        parts.append(deploys[0])
    elif summary:
        parts.append(summary[:_TITLE_SUMMARY_LEN])
    return " — ".join(parts)


def _build_card_body(
    task_title: str,
    summary: str,
    deploys: list[str],
    commits: list[str],
) -> str:
    lines: list[str] = []
    if task_title:
        lines.append(task_title)
        lines.append("")
    if summary:
        lines.append(summary)
        lines.append("")
    if deploys:
        lines.append("**Deployed:**")
        lines.extend(f"- {url}" for url in deploys)
        lines.append("")
    if commits:
        lines.append("**Commits:** " + ", ".join(f"`{c}`" for c in commits))
    return "\n".join(lines).rstrip() or task_title or ""


def emit_task_shipped_card(
    svc: _TaskShippedSvc,
    task_id: str,
    actor: str,
) -> str | None:
    """Create the operator inbox card announcing a shipped task.

    Returns the new card's task_id, or None if there's nothing to
    announce (no work_output, lookup failure, or emit failure). This
    function is best-effort — it logs and swallows so a broken inbox
    surface never blocks the primary task_done transition.

    The card itself is a chat-flow task with a stable label set
    (``task_shipped``, plus ``has_deliverable_url`` when applicable)
    so the cockpit inbox renderer can recognize it without parsing
    the body.
    """
    try:
        work_output = _latest_work_output(svc, task_id)
        if work_output is None:
            return None
        try:
            task = svc.get(task_id)
        except Exception:  # noqa: BLE001
            return None

        summary = (getattr(work_output, "summary", "") or "").strip()
        commits = _extract_commit_refs(work_output)
        deploys = _extract_deploy_urls(work_output)

        title = _build_card_title(task.task_id, summary, deploys)
        body = _build_card_body(
            getattr(task, "title", "") or "",
            summary,
            deploys,
            commits,
        )

        labels = [_TASK_SHIPPED_LABEL]
        if deploys:
            labels.append(_HAS_DELIVERABLE_URL_LABEL)

        card = svc.create(
            title=title,
            description=body,
            type="task",
            project=task.project,
            flow_template="chat",
            roles={"requester": "user", "operator": actor or "polly"},
            priority="normal",
            created_by=actor or "polly",
            labels=labels,
        )
        return getattr(card, "task_id", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "task_shipped emit skipped for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        return None


__all__ = ["emit_task_shipped_card"]
