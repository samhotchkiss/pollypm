"""Apply-step routing — approve → commit/merge, reject → archive.

Spec §6 routing table:

    category           | on approval               | on rejection
    ------------------ | ------------------------- | ---------------------------
    spec_feature       | move ideas/→specs/; merge | archive ideas/ under archive/
    build_speculative  | create non-draft PR       | archive branch ref, delete
    audit_docs         | convert draft PR→ready    | close PR, archive
    security_scan      | stamp report reviewed     | stamp report dismissed
    try_alt_approach   | keep branch, close task   | archive branch ref, delete

Apply functions are pure Python — they produce **ApplyAction** records
that describe what the caller should do (file moves, branch archives,
GH API calls). The wrapper ``run_apply`` actually executes the side
effects. Keeping the action layer separate makes the approve/reject
policy testable without a live git repo or a GH server.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.downtime.handlers.security_scan import (
    validate_no_source_changes,
)


logger = logging.getLogger(__name__)


ARCHIVE_DIR = Path(".pollypm") / "archive"


# ---------------------------------------------------------------------------
# Action dataclass — describes a single intent.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ApplyAction:
    """A single side-effectful step the apply handler plans to perform.

    ``verb`` is a machine-readable tag; ``detail`` is the human summary
    written into the work-service context log.

    Known verbs:
      * ``"move_file"`` — move a file from ``src`` to ``dst``.
      * ``"archive_file"`` — move a file under ``archive/``.
      * ``"archive_branch"`` — rename the branch to ``archive/<name>``.
      * ``"keep_branch"`` — leave the branch untouched.
      * ``"pr_ready"`` — flip a draft PR to ready.
      * ``"pr_close"`` — close a PR.
      * ``"stamp_report"`` — append a review/dismiss stamp to a report.
      * ``"pr_create"`` — open a non-draft PR from the branch.
    """

    verb: str
    detail: str
    src: str | None = None
    dst: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ApplyPlan:
    """Full plan for a single apply invocation."""

    approved: bool
    actions: tuple[ApplyAction, ...] = ()
    refused: bool = False
    refusal_reason: str = ""


# ---------------------------------------------------------------------------
# Plan builders — pure, one per category + approval state.
# ---------------------------------------------------------------------------


def plan_spec_feature(*, approved: bool, result: dict[str, Any]) -> ApplyPlan:
    slug = str(result.get("slug") or "")
    artifact = str(result.get("artifact_path") or "")
    branch = str(result.get("branch_name") or "")
    if approved:
        dst = (Path("docs") / "specs" / f"{slug}.md").as_posix()
        actions = (
            ApplyAction(
                verb="move_file",
                detail=f"Approved: promote spec from {artifact} → {dst}.",
                src=artifact,
                dst=dst,
            ),
            ApplyAction(
                verb="pr_create",
                detail=f"Approved: open PR from branch {branch}.",
                src=branch,
            ),
        )
    else:
        dst = (ARCHIVE_DIR / "specs" / f"{slug}.md").as_posix()
        actions = (
            ApplyAction(
                verb="archive_file",
                detail=f"Rejected: archive {artifact} → {dst}.",
                src=artifact,
                dst=dst,
            ),
            ApplyAction(
                verb="archive_branch",
                detail=f"Rejected: rename branch {branch} → archive/{branch}.",
                src=branch,
            ),
        )
    return ApplyPlan(approved=approved, actions=actions)


def plan_build_speculative(*, approved: bool, result: dict[str, Any]) -> ApplyPlan:
    branch = str(result.get("branch_name") or "")
    if approved:
        actions = (
            ApplyAction(
                verb="pr_create",
                detail=(
                    f"Approved: open a non-draft PR from {branch}. "
                    "User can `gh pr merge` when ready."
                ),
                src=branch,
            ),
        )
    else:
        actions = (
            ApplyAction(
                verb="archive_branch",
                detail=f"Rejected: archive branch {branch}.",
                src=branch,
            ),
        )
    return ApplyPlan(approved=approved, actions=actions)


def plan_audit_docs(*, approved: bool, result: dict[str, Any]) -> ApplyPlan:
    branch = str(result.get("branch_name") or "")
    pr_number = result.get("pr_number")
    pr_title = str(result.get("pr_title") or "")
    pr_body = str(result.get("pr_body") or "")
    if approved:
        actions = (
            ApplyAction(
                verb="pr_ready",
                detail=(
                    f"Approved: flip PR to ready for review "
                    f"(PR #{pr_number or '?'} on {branch})."
                ),
                src=branch,
                extras={"pr_number": pr_number, "pr_title": pr_title, "pr_body": pr_body},
            ),
        )
    else:
        actions = (
            ApplyAction(
                verb="pr_close",
                detail=(
                    f"Rejected: close PR #{pr_number or '?'} on {branch}."
                ),
                src=branch,
                extras={"pr_number": pr_number},
            ),
        )
    return ApplyPlan(approved=approved, actions=actions)


def plan_security_scan(
    *,
    approved: bool,
    result: dict[str, Any],
    changed_paths: list[str] | None = None,
) -> ApplyPlan:
    report = str(result.get("report_path") or "")
    # Spec §10 layer-3: refuse to apply if ANY file outside the reports
    # directory was modified. The caller passes the list of paths touched
    # during the exploration — this is the load-bearing check that makes
    # security-scan report-only even if the persona tries to write code.
    ok, offenders = validate_no_source_changes(changed_paths=changed_paths or [])
    if not ok:
        return ApplyPlan(
            approved=approved,
            refused=True,
            refusal_reason=(
                "Security-scan task modified files outside "
                ".pollypm/security-reports/ — refusing to apply. "
                f"Offending paths: {offenders!r}"
            ),
        )
    stamp_verb = "reviewed" if approved else "dismissed"
    stamp_text = (
        f"\n> Reviewed on {_date.today().isoformat()}\n"
        if approved
        else f"\n> Dismissed on {_date.today().isoformat()}\n"
    )
    actions = (
        ApplyAction(
            verb="stamp_report",
            detail=(
                f"{'Approved' if approved else 'Rejected'}: stamp "
                f"{report} as {stamp_verb}."
            ),
            src=report,
            extras={"stamp_text": stamp_text},
        ),
    )
    return ApplyPlan(approved=approved, actions=actions)


def plan_try_alt_approach(*, approved: bool, result: dict[str, Any]) -> ApplyPlan:
    branch = str(result.get("branch_name") or "")
    if approved:
        actions = (
            ApplyAction(
                verb="keep_branch",
                detail=(
                    f"Approved: keep branch {branch} for consideration. "
                    "User decides adoption manually."
                ),
                src=branch,
            ),
        )
    else:
        actions = (
            ApplyAction(
                verb="archive_branch",
                detail=f"Rejected: archive branch {branch}.",
                src=branch,
            ),
        )
    return ApplyPlan(approved=approved, actions=actions)


_KIND_TO_PLANNER = {
    "spec_feature": plan_spec_feature,
    "build_speculative": plan_build_speculative,
    "audit_docs": plan_audit_docs,
    "try_alt_approach": plan_try_alt_approach,
}


def build_apply_plan(
    *,
    kind: str,
    approved: bool,
    result: dict[str, Any],
    changed_paths: list[str] | None = None,
) -> ApplyPlan:
    """Return the plan for the given (kind, approval) pair.

    security_scan takes the extra ``changed_paths`` kwarg so callers
    can feed the apply-time refusal check. Unknown kinds raise —
    mirrors the dispatch router.
    """
    if kind == "security_scan":
        return plan_security_scan(
            approved=approved, result=result, changed_paths=changed_paths
        )
    planner = _KIND_TO_PLANNER.get(kind)
    if planner is None:
        raise ValueError(f"Unknown downtime kind: {kind!r}")
    return planner(approved=approved, result=result)


# ---------------------------------------------------------------------------
# Execution — takes an ApplyPlan and performs the actions on disk.
# ---------------------------------------------------------------------------


def execute_plan(
    plan: ApplyPlan, *, project_root: Path
) -> list[str]:
    """Run every action in the plan. Returns per-action summary strings.

    Idempotent-ish: missing source files are logged and skipped rather
    than raising. PR / branch operations that need ``gh`` or ``git``
    are attempted best-effort; callers that need stricter guarantees
    should check the returned summary list for ``failed:`` prefixes.
    """
    summaries: list[str] = []
    if plan.refused:
        summaries.append(f"refused: {plan.refusal_reason}")
        return summaries

    for action in plan.actions:
        summary = _execute_action(action, project_root=project_root)
        summaries.append(summary)
    return summaries


def _execute_action(action: ApplyAction, *, project_root: Path) -> str:
    try:
        if action.verb == "move_file":
            return _move_file(project_root, action)
        if action.verb == "archive_file":
            return _archive_file(project_root, action)
        if action.verb == "archive_branch":
            return _archive_branch(project_root, action)
        if action.verb == "keep_branch":
            return f"ok: kept branch {action.src}"
        if action.verb == "stamp_report":
            return _stamp_report(project_root, action)
        if action.verb == "pr_create":
            return f"planned: open PR for {action.src} (gh invocation TBD)"
        if action.verb == "pr_ready":
            return f"planned: flip PR to ready for {action.src}"
        if action.verb == "pr_close":
            return f"planned: close PR on {action.src}"
    except Exception as exc:  # noqa: BLE001 - execution must not crash the tick
        logger.warning("downtime apply: action %s failed: %s", action.verb, exc)
        return f"failed: {action.verb} — {exc}"
    return f"skipped: unknown verb {action.verb!r}"


def _move_file(project_root: Path, action: ApplyAction) -> str:
    if not action.src or not action.dst:
        return "failed: move_file missing src or dst"
    src = project_root / action.src
    dst = project_root / action.dst
    if not src.exists():
        return f"skipped: {action.src} missing (already moved?)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"ok: moved {action.src} → {action.dst}"


def _archive_file(project_root: Path, action: ApplyAction) -> str:
    if not action.src or not action.dst:
        return "failed: archive_file missing src or dst"
    src = project_root / action.src
    dst = project_root / action.dst
    if not src.exists():
        return f"skipped: {action.src} missing"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"ok: archived {action.src} → {action.dst}"


def _archive_branch(project_root: Path, action: ApplyAction) -> str:
    if not action.src:
        return "failed: archive_branch missing src"
    from pollypm.plugins_builtin.downtime.handlers.spec_feature import (
        _is_git_repo,
        _run_git,
    )

    if not _is_git_repo(project_root):
        return f"skipped: {project_root} is not a git repo"
    new_name = f"archive/{action.src}"
    # git branch -m <old> <new> renames the branch. Use -M to force
    # overwrite if the archive ref happens to exist already.
    code, out = _run_git(["branch", "-M", action.src, new_name], cwd=project_root)
    if code != 0:
        return f"failed: branch rename {action.src} → {new_name}: {out}"
    return f"ok: renamed {action.src} → {new_name}"


def _stamp_report(project_root: Path, action: ApplyAction) -> str:
    if not action.src:
        return "failed: stamp_report missing src"
    path = project_root / action.src
    if not path.exists():
        return f"failed: report {action.src} missing"
    stamp_text = str(action.extras.get("stamp_text") or "")
    if stamp_text:
        existing = path.read_text()
        if stamp_text.strip() in existing:
            return f"skipped: {action.src} already stamped"
        path.write_text(existing.rstrip() + stamp_text)
    return f"ok: stamped {action.src}"


# ---------------------------------------------------------------------------
# Convenience wrapper — the apply-node entry point.
# ---------------------------------------------------------------------------


def run_apply(
    *,
    kind: str,
    approved: bool,
    result: dict[str, Any],
    project_root: Path,
    changed_paths: list[str] | None = None,
) -> tuple[ApplyPlan, list[str]]:
    """Build the plan, execute it, return both for logging."""
    plan = build_apply_plan(
        kind=kind, approved=approved, result=result, changed_paths=changed_paths
    )
    summaries = execute_plan(plan, project_root=project_root)
    return plan, summaries
