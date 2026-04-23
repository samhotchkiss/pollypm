"""Advisor session context packer + output parser.

ad03 wires three things:

1. ``build_context_pack(...)`` — reads the plan, the delta (from ad02's
   ChangeReport), and the trajectory (last 3 advisor decisions from the
   history log + recent dismissals from advisor-state.json), then
   writes a single ``.pollypm-advisor-context.md`` in the project
   root — the advisor session reads that file as its context.

2. ``parse_decision(output)`` — extracts the structured JSON decision
   from the session's work-output text and validates it against the
   schema from spec §5. Malformed input → a synthetic silent decision
   with ``rationale_if_silent="invalid-output: …"`` so the tick never
   crashes and the audit log is still truthful.

3. ``run_assess(...)`` — glue that kicks a ChangeReport through
   build_context_pack, writes it next to the advisor task's worktree,
   and returns the path + the computed pack structure.

The actual session launch (``pm task claim`` + worker spawn) lives in
the tick handler's enqueue path — ad03 just packages the context and
exposes the output-parsing surface. ad04 wires the history-log
append; ad05 wires the inbox emission.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.atomic_io import atomic_write_text
from pollypm.plugins_builtin.advisor.handlers.detect_changes import (
    ChangeReport,
    TaskTransitionRecord,
)
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    recent_entries_for_project,
)
from pollypm.plugins_builtin.advisor.state import (
    Dismissal,
    ProjectAdvisorState,
    load_state,
)


logger = logging.getLogger(__name__)


CONTEXT_FILENAME = ".pollypm-advisor-context.md"
PLAN_RELATIVE_PATH = "docs/project-plan.md"
# Candidates tried in order by :func:`_read_plan`. Mirrors the
# plan-presence gate list in
# :mod:`pollypm.plugins_builtin.project_planning.plan_presence` so the
# advisor anchors against whichever canonical plan file the project
# actually uses.
PLAN_RELATIVE_CANDIDATES: tuple[str, ...] = (
    PLAN_RELATIVE_PATH,
    "docs/plan/plan.md",
)
MAX_CONTEXT_CHARS = 20_000
MAX_PLAN_CHARS = 8_000
MAX_PER_FILE_DIFF_LINES = 500
MAX_TOTAL_DIFF_CHARS = 6_000

VALID_SEVERITIES = frozenset({"suggestion", "recommendation", "critical"})
VALID_TOPICS = frozenset(
    {
        "architecture_drift",
        "missing_tests",
        "dependency_risk",
        "plan_divergence",
        "pattern_emerging",
        "risk_materializing",
        "other",
    }
)


# ---------------------------------------------------------------------------
# Data classes — the pack as a structured object, cheap to introspect.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextPack:
    """The three inputs the advisor session gets, pre-rendered."""

    project_key: str
    plan_text: str
    delta_section: str
    trajectory_section: str
    markdown: str


@dataclass(slots=True)
class AdvisorDecision:
    """Parsed form of the advisor session's structured JSON output."""

    emit: bool
    topic: str | None = None
    severity: str | None = None
    summary: str = ""
    details: str = ""
    suggestion: str = ""
    rationale_if_silent: str = ""
    # Set when we substituted a silent decision in for malformed output.
    invalid_output_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "emit": self.emit,
            "rationale_if_silent": self.rationale_if_silent,
        }
        if self.emit:
            data.update(
                {
                    "topic": self.topic,
                    "severity": self.severity,
                    "summary": self.summary,
                    "details": self.details,
                    "suggestion": self.suggestion,
                }
            )
        if self.invalid_output_reason:
            data["invalid_output_reason"] = self.invalid_output_reason
        return data


# ---------------------------------------------------------------------------
# Pack 1 — plan
# ---------------------------------------------------------------------------


def _read_plan(project_path: Path) -> str:
    """Read the project plan (truncated to MAX_PLAN_CHARS).

    Tries each entry in :data:`PLAN_RELATIVE_CANDIDATES` in order so
    projects that use the older ``docs/plan/plan.md`` convention
    instead of ``docs/project-plan.md`` still get a plan anchor.
    """
    plan_path: Path | None = None
    for relative in PLAN_RELATIVE_CANDIDATES:
        candidate = project_path / relative
        if candidate.exists():
            plan_path = candidate
            break
    if plan_path is None:
        joined = " or ".join(PLAN_RELATIVE_CANDIDATES)
        return f"(no {joined} — advisor running without a plan anchor)"
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("advisor: failed to read plan %s: %s", plan_path, exc)
        return f"(failed to read plan: {exc})"
    if len(text) > MAX_PLAN_CHARS:
        return text[:MAX_PLAN_CHARS] + "\n\n[plan truncated — exceeded context budget]"
    return text


# ---------------------------------------------------------------------------
# Pack 2 — delta
# ---------------------------------------------------------------------------


def _git_diff_for_file(
    project_path: Path,
    file_path: Path,
    commit_shas: list[str],
    *,
    timeout: float = 10.0,
) -> str:
    """Return truncated diff text for one file across the commit window.

    Uses ``git diff <earliest>^..HEAD -- <file>`` to get the full
    cumulative delta. Truncates per spec to MAX_PER_FILE_DIFF_LINES
    so a huge refactor doesn't blow the context budget.
    """
    if not commit_shas:
        return ""
    earliest = commit_shas[0]
    cmd = ["git", "-C", str(project_path), "diff",
           f"{earliest}^..HEAD", "--", str(file_path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("advisor: diff failed for %s: %s", file_path, exc)
        return ""
    if result.returncode != 0:
        # Fallback: show the latest HEAD commit's view of the file.
        try:
            result = subprocess.run(
                ["git", "-C", str(project_path), "show",
                 commit_shas[-1], "--", str(file_path)],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except Exception:  # noqa: BLE001
            return ""
        if result.returncode != 0:
            return ""
    lines = result.stdout.splitlines()
    if len(lines) > MAX_PER_FILE_DIFF_LINES:
        lines = lines[:MAX_PER_FILE_DIFF_LINES]
        lines.append(f"[diff truncated at {MAX_PER_FILE_DIFF_LINES} lines]")
    return "\n".join(lines)


def _render_delta_section(
    report: ChangeReport,
    *,
    project_path: Path,
    include_diffs: bool = True,
) -> str:
    """Render the delta (commits, files, transitions + diffs) as markdown."""
    parts: list[str] = []
    parts.append("## Delta since last advisor run")
    parts.append("")
    if report.since is None:
        parts.append("First advisor run for this project (24-hour lookback).")
    else:
        parts.append(f"Since: `{report.since.isoformat()}`")
    parts.append("")
    parts.append(f"**Summary:** {report.files_diff_summary or '(no activity)'}")
    parts.append("")

    if report.commit_shas:
        parts.append("### Commits")
        for sha in report.commit_shas:
            parts.append(f"- `{sha}`")
        parts.append("")

    if report.changed_files:
        parts.append("### Changed files")
        for f in report.changed_files:
            parts.append(f"- `{f}`")
        parts.append("")

    if report.task_transitions:
        parts.append("### Task transitions")
        for t in report.task_transitions:
            parts.append(
                f"- `{t.task_id}`: {t.from_state} -> {t.to_state} "
                f"(actor={t.actor or 'unknown'}, at {t.timestamp})"
            )
        parts.append("")

    if include_diffs and report.commit_shas and report.changed_files:
        parts.append("### Diffs (truncated)")
        parts.append("")
        total_chars = 0
        for f in report.changed_files:
            diff_text = _git_diff_for_file(project_path, f, report.commit_shas)
            if not diff_text:
                continue
            block = f"#### `{f}`\n\n```diff\n{diff_text}\n```\n"
            if total_chars + len(block) > MAX_TOTAL_DIFF_CHARS:
                parts.append(
                    "[remaining per-file diffs truncated — context budget reached]"
                )
                break
            parts.append(block)
            total_chars += len(block)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pack 3 — trajectory
# ---------------------------------------------------------------------------


def _render_trajectory_section(
    entries: list[HistoryEntry],
    *,
    dismissals: list[Dismissal],
) -> str:
    """Render the last-3 decisions + recent dismissals as markdown.

    The advisor persona explicitly respects user dismissals in its
    rules — so we surface both the prior decisions (emit or silent,
    both) and any `topic_cooldown` rejections.
    """
    parts: list[str] = ["## Your trajectory on this project", ""]
    if not entries:
        parts.append("(no prior advisor runs on this project)")
    else:
        parts.append("### Last 3 decisions (oldest first)")
        parts.append("")
        for e in entries:
            header = f"- **{e.timestamp}** — `{e.decision}`"
            if e.decision == "emit":
                header += f" — topic: `{e.topic or 'other'}`, severity: `{e.severity or 'suggestion'}`"
            parts.append(header)
            if e.decision == "emit":
                parts.append(f"  - summary: {e.summary}")
            else:
                parts.append(f"  - rationale: {e.rationale_if_silent or '(none recorded)'}")
        parts.append("")

    if dismissals:
        parts.append("### Recent user dismissals")
        parts.append(
            "The user rejected the following advisor insights with "
            "`--reason topic_cooldown`. Respect these signals — stay "
            "silent on these topics unless upgrading severity."
        )
        parts.append("")
        for d in dismissals:
            parts.append(f"- topic `{d.topic}` — rejected at {d.at}")
        parts.append("")
    else:
        parts.append("(no recent user dismissals)")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pack assembly
# ---------------------------------------------------------------------------


def build_context_pack(
    *,
    project_key: str,
    project_path: Path,
    report: ChangeReport,
    base_dir: Path,
    include_diffs: bool = True,
) -> ContextPack:
    """Build a ContextPack from plan + delta + trajectory.

    Trajectory reads the advisor log + state for ``project_key``. If
    either is empty (first-run projects) the trajectory section still
    renders — it just says "no prior runs" so the advisor isn't
    confused by a missing section.
    """
    plan_text = _read_plan(project_path)

    trajectory_entries = recent_entries_for_project(base_dir, project_key, limit=3)
    state = load_state(base_dir)
    proj_state: ProjectAdvisorState = state.get(project_key)
    dismissals = list(proj_state.recent_dismissals)

    delta_section = _render_delta_section(
        report, project_path=project_path, include_diffs=include_diffs,
    )
    trajectory_section = _render_trajectory_section(
        trajectory_entries, dismissals=dismissals,
    )

    # Final markdown. The persona's <system> block instructs the advisor
    # to read this file — if we ever rename it, update the persona too.
    parts = [
        f"# Advisor context pack for `{project_key}`",
        "",
        f"Generated at {datetime.now(UTC).isoformat()}",
        "",
        "## The plan (north star)",
        "",
        plan_text,
        "",
        delta_section,
        "",
        trajectory_section,
        "",
        "## Your task",
        "",
        "Read the plan, the delta, and your trajectory. Apply the rules in "
        "your persona carefully. Produce a single JSON object — emit or "
        "silent — per the schema in your persona's <output_format>. "
        "Nothing else. No prose around the JSON.",
        "",
    ]
    markdown = "\n".join(parts)
    if len(markdown) > MAX_CONTEXT_CHARS:
        markdown = markdown[:MAX_CONTEXT_CHARS] + "\n\n[context truncated]"

    return ContextPack(
        project_key=project_key,
        plan_text=plan_text,
        delta_section=delta_section,
        trajectory_section=trajectory_section,
        markdown=markdown,
    )


def write_context_file(project_path: Path, pack: ContextPack) -> Path:
    """Write the pack to ``<project_path>/.pollypm-advisor-context.md``.

    Atomic write so a concurrent reader (the advisor session spinning
    up) never sees a half-written file. Returns the path.
    """
    target = Path(project_path) / CONTEXT_FILENAME
    atomic_write_text(target, pack.markdown)
    return target


def run_assess(
    *,
    project_key: str,
    project_path: Path,
    report: ChangeReport,
    base_dir: Path,
) -> dict[str, Any]:
    """End-to-end: build pack, persist to disk, return summary.

    Called by the tick handler's enqueue path (ad01 stub replaced in
    ad03) once a project has been identified as changed. Does NOT
    launch the session — task creation + worker spawn happens through
    the work service by the caller.
    """
    pack = build_context_pack(
        project_key=project_key,
        project_path=project_path,
        report=report,
        base_dir=base_dir,
    )
    context_path = write_context_file(project_path, pack)
    return {
        "context_path": str(context_path),
        "context_chars": len(pack.markdown),
        "commit_count": len(report.commit_shas),
        "file_count": len(report.changed_files),
        "transition_count": len(report.task_transitions),
    }


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_first_json_object(text: str) -> str | None:
    """Return the first JSON object substring found in ``text``.

    The advisor persona is instructed to emit bare JSON, but some
    providers wrap output in markdown fences. This helper accepts
    either shape, plus a last-resort brace-balance fallback.
    """
    if not text:
        return None
    # Try fenced code block first.
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group(1)
    # Bare JSON — find the first `{` and balance braces.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_decision(output: str | None) -> AdvisorDecision:
    """Parse the session's work-output text into an AdvisorDecision.

    Validation per spec §5:

    * ``emit`` must be a bool.
    * When ``emit=true``, ``topic`` / ``severity`` / ``summary`` /
      ``details`` / ``suggestion`` are required. Missing / invalid
      fields coerce the decision to silent with an
      ``invalid_output_reason``.
    * ``rationale_if_silent`` is required for silent decisions.

    Malformed output never raises — the tick must never crash on a
    flaky session. Instead we return a silent AdvisorDecision whose
    ``invalid_output_reason`` spells out what went wrong; ad04 logs
    that reason, and the operator catches it via `pm advisor history`.
    """
    if output is None or not output.strip():
        return AdvisorDecision(
            emit=False,
            rationale_if_silent="invalid-output: empty session output",
            invalid_output_reason="empty",
        )

    json_text = _extract_first_json_object(output)
    if json_text is None:
        return AdvisorDecision(
            emit=False,
            rationale_if_silent="invalid-output: no JSON object found in session output",
            invalid_output_reason="no-json",
        )

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return AdvisorDecision(
            emit=False,
            rationale_if_silent=f"invalid-output: JSON decode error ({exc.msg})",
            invalid_output_reason=f"json-decode: {exc.msg}",
        )

    if not isinstance(data, dict):
        return AdvisorDecision(
            emit=False,
            rationale_if_silent="invalid-output: JSON was not an object",
            invalid_output_reason="not-object",
        )

    emit_raw = data.get("emit")
    if not isinstance(emit_raw, bool):
        return AdvisorDecision(
            emit=False,
            rationale_if_silent="invalid-output: `emit` must be a boolean",
            invalid_output_reason="emit-not-bool",
        )

    if emit_raw:
        topic = data.get("topic") if isinstance(data.get("topic"), str) else None
        severity = data.get("severity") if isinstance(data.get("severity"), str) else None
        summary = str(data.get("summary") or "").strip()
        details = str(data.get("details") or "").strip()
        suggestion = str(data.get("suggestion") or "").strip()

        missing: list[str] = []
        if not topic:
            missing.append("topic")
        if not severity or severity not in VALID_SEVERITIES:
            missing.append("severity")
        if not summary:
            missing.append("summary")
        if not details:
            missing.append("details")
        if not suggestion:
            missing.append("suggestion")

        if missing:
            return AdvisorDecision(
                emit=False,
                rationale_if_silent=(
                    f"invalid-output: emit=true but missing/invalid "
                    f"fields: {', '.join(missing)}"
                ),
                invalid_output_reason=f"emit-missing: {','.join(missing)}",
            )
        # Topic validation — unknown topics degrade to "other" rather
        # than killing the emit, since the persona may suggest a valid
        # observation under a topic we haven't enumerated yet.
        if topic not in VALID_TOPICS:
            topic = "other"
        return AdvisorDecision(
            emit=True,
            topic=topic,
            severity=severity,
            summary=summary,
            details=details,
            suggestion=suggestion,
            rationale_if_silent="",
        )

    # emit == False
    rationale = str(data.get("rationale_if_silent") or "").strip()
    if not rationale:
        return AdvisorDecision(
            emit=False,
            rationale_if_silent="invalid-output: emit=false but no rationale_if_silent provided",
            invalid_output_reason="silent-no-rationale",
        )
    return AdvisorDecision(emit=False, rationale_if_silent=rationale)


# ---------------------------------------------------------------------------
# Post-session wiring — history append + optional inbox emit.
# ---------------------------------------------------------------------------


def record_decision(
    *,
    base_dir: Path,
    project: str,
    decision: AdvisorDecision,
    task_id: str = "",
    commits_reviewed: list[str] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Persist the session's decision: history log (always) + inbox (if emit).

    Returns a dict with ``history_entry`` (the HistoryEntry that was
    appended) and ``inbox_entry`` (the ``AdvisorInsight`` written, or
    None on silent / invalid-output decisions).

    Called by the assess path on session completion — the tick handler
    enqueues; ad03 runs the session; this wires the terminal handling
    for ad04 (log) + ad05 (inbox). Kept in assess.py so the emit path
    is one unit the operator can reason about.
    """
    from pollypm.plugins_builtin.advisor.handlers.history_log import (
        append_decision,
    )

    ts = (now_utc or datetime.now(UTC)).isoformat()
    history_entry = append_decision(
        base_dir,
        project=project,
        decision_json=decision.to_dict(),
        task_id=task_id,
        commits_reviewed=commits_reviewed or [],
        timestamp=ts,
    )

    inbox_entry = None
    if decision.emit and not decision.invalid_output_reason:
        from pollypm.plugins_builtin.advisor.inbox import emit_insight

        try:
            inbox_entry = emit_insight(
                base_dir,
                project=project,
                decision=decision,
                task_id=task_id,
                commits_reviewed=commits_reviewed or [],
                now_utc=now_utc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisor: inbox emit failed: %s", exc)

    return {"history_entry": history_entry, "inbox_entry": inbox_entry}
