"""Herald synthesis + structured fallback + quiet-mode detection.

This module turns the mb02 structured data into the briefing the user
will actually read. Three paths:

1. **Synthesized briefing** — a pluggable ``HeraldInvocation`` callable
   is given a Markdown context pack and asked to return a single JSON
   object. If we parse it cleanly, that's the briefing.

2. **Fallback briefing** — if the herald errors, times out, or returns
   something unparseable, we build a structured-but-unnarrated briefing
   directly from the data. Body is prefixed with a warning so the user
   knows it's the fallback. Inbox still gets something.

3. **Quiet mode** — if the last 7 days (configurable) had zero commits,
   transitions, insights, and downtime events across all projects, the
   plugin downshifts to weekly cadence (Sundays only). Tracked via
   ``BriefingState.last_quiet_weekly_date`` — mb04 uses this to decide
   whether to actually emit on quiet-mode days.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from pollypm.atomic_io import atomic_write_text
from pollypm.plugins_builtin.morning_briefing.handlers.gather_yesterday import (
    YesterdaySnapshot,
    gather_yesterday,
)
from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
    PriorityList,
    identify_priorities,
)
from pollypm.plugins_builtin.morning_briefing.state import BriefingState


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Briefing-log path + size cap
# ---------------------------------------------------------------------------


BRIEFING_LOG_NAME = "briefing-log.jsonl"
BRIEFING_LOG_MAX_ENTRIES = 90          # ~3 months of daily briefings


# ---------------------------------------------------------------------------
# Structured briefing dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PriorityLine:
    title: str
    project: str
    why: str = ""


@dataclass(slots=True)
class BriefingDraft:
    """What the briefing carries before inbox emission.

    ``mode`` is ``"synthesized"`` when the herald produced the text,
    ``"fallback"`` when we generated it ourselves, and
    ``"quiet-mode"`` when we're in vacation mode and emitted a single
    short "silence" note.
    """

    date_local: str
    mode: str
    yesterday: str = ""
    priorities: list[PriorityLine] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    markdown: str = ""
    meta: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Herald invocation protocol
# ---------------------------------------------------------------------------


class HeraldInvocation(Protocol):
    """Callable used to spawn the herald session.

    Implementations: production uses a short-lived session worker; tests
    use stubs. The function receives the Markdown context pack and the
    target budget in seconds, and must return the herald's raw text
    (expected to be JSON).

    Must raise on timeout / provider error so the caller can fall back.
    """

    def __call__(self, context_md: str, *, budget_seconds: int) -> str: ...


# Default herald invocation — not wired to a real session service in
# this module. The plugin host can install a real one via:
#
#     from pollypm.plugins_builtin.morning_briefing.handlers import synthesize
#     synthesize.herald_invocation = my_real_herald
#
# Tests override this attribute directly.
def _default_herald_invocation(context_md: str, *, budget_seconds: int) -> str:
    raise NotImplementedError(
        "morning_briefing: no herald invocation installed. "
        "Set pollypm.plugins_builtin.morning_briefing.handlers.synthesize.herald_invocation "
        "to a HeraldInvocation before enabling synthesis."
    )


herald_invocation: HeraldInvocation = _default_herald_invocation


# ---------------------------------------------------------------------------
# Briefing log
# ---------------------------------------------------------------------------


def briefing_log_path(base_dir: Path) -> Path:
    return Path(base_dir) / BRIEFING_LOG_NAME


def append_briefing_log(base_dir: Path, entry: dict) -> None:
    """Append one JSONL line to the briefing log; cap the file size."""
    path = briefing_log_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if path.exists():
        try:
            existing = path.read_text().splitlines()
        except OSError:
            existing = []
    existing.append(json.dumps(entry, sort_keys=True))
    # Trim to the last N entries so the log is bounded.
    if len(existing) > BRIEFING_LOG_MAX_ENTRIES:
        existing = existing[-BRIEFING_LOG_MAX_ENTRIES:]
    atomic_write_text(path, "\n".join(existing) + "\n")


def load_recent_briefings(base_dir: Path, *, limit: int = 3) -> list[dict]:
    """Return the N most recent log entries (newest first)."""
    path = briefing_log_path(base_dir)
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Context pack (Markdown handed to the herald)
# ---------------------------------------------------------------------------


def build_context_md(
    *,
    snapshot: YesterdaySnapshot,
    priorities: PriorityList,
    recent: list[dict],
) -> str:
    """Render the structured inputs as a Markdown context pack for the herald."""
    lines: list[str] = []
    lines.append(f"# Morning Briefing Context — {snapshot.date_local}\n")
    lines.append("## Yesterday")
    lines.append(f"- Window (UTC): {snapshot.window_start_utc} → {snapshot.window_end_utc}")
    lines.append(f"- Total commits: {snapshot.total_commits()}")
    lines.append(f"- Task transitions: {len(snapshot.task_transitions)}")
    lines.append(f"- Advisor insights: {len(snapshot.advisor_insights)}")
    lines.append(f"- Downtime artifacts: {len(snapshot.downtime_artifacts)}")
    lines.append("")

    if snapshot.commits_by_project:
        lines.append("### Commits")
        for project_key, commits in snapshot.commits_by_project.items():
            if not commits:
                continue
            lines.append(f"**{project_key}** ({len(commits)}):")
            for c in commits[:10]:
                lines.append(f"- `{c.sha[:8]}` {c.subject} — {c.author}")
            lines.append("")

    if snapshot.task_transitions:
        lines.append("### Task transitions")
        for t in snapshot.task_transitions[:30]:
            lines.append(
                f"- {t.task_id}: {t.from_state} → {t.to_state} "
                f"({t.actor}) — {t.task_title}"
            )
        lines.append("")

    if snapshot.advisor_insights:
        lines.append("### Advisor insights")
        for i in snapshot.advisor_insights:
            lines.append(f"- [{i.kind}] {i.title}: {i.body[:200]}")
        lines.append("")

    if snapshot.downtime_artifacts:
        lines.append("### Downtime awaiting approval")
        for d in snapshot.downtime_artifacts:
            lines.append(f"- {d.task_id}: {d.title}")
        lines.append("")

    lines.append("## Today's priorities (ranked)")
    if not priorities.top_tasks:
        lines.append("- (none)")
    else:
        for p in priorities.top_tasks:
            age_h = p.age_seconds / 3600.0
            lines.append(
                f"- [{p.priority}] {p.task_id}: {p.title} "
                f"(state={p.state}, assignee={p.assignee or 'unassigned'}, "
                f"stale={age_h:.1f}h)"
            )
    lines.append("")

    if priorities.blockers:
        lines.append("### Blockers")
        for b in priorities.blockers:
            refs = ", ".join(b.unresolved_blockers) or "(none unresolved)"
            lines.append(f"- {b.task_id}: {b.title} — blocked by {refs}")
        lines.append("")

    if priorities.awaiting_approval:
        lines.append("### Awaiting approval (>24h)")
        for a in priorities.awaiting_approval:
            lines.append(f"- {a.id}: {a.subject} ({a.kind}, {a.age_hours:.1f}h)")
        lines.append("")

    if recent:
        lines.append("## Your last 3 briefings (do not repeat framing)")
        for r in recent:
            ts = r.get("timestamp", "?")
            yesterday = r.get("yesterday", "")
            lines.append(f"- {ts}: {str(yesterday)[:160]}")
        lines.append("")

    lines.append("## Output")
    lines.append(
        "Return a single JSON object — no prose, no code fences. Schema:"
    )
    lines.append("```")
    lines.append(
        '{\n'
        '  "yesterday": "2-4 sentence narrative",\n'
        '  "priorities": [\n'
        '    {"title": "...", "project": "...", "why": "one-sentence rationale"}\n'
        '  ],\n'
        '  "watch": ["optional 0-2 bullets"]\n'
        '}'
    )
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Herald-output parsing
# ---------------------------------------------------------------------------


def parse_herald_output(raw: str) -> dict:
    """Parse the herald's text output.

    Tolerates code fences (```json ... ```) wrapping the JSON, which is
    a common LLM pattern even when prompted otherwise.
    """
    if raw is None:
        raise ValueError("herald output was None")
    text = raw.strip()
    if not text:
        raise ValueError("herald output was empty")

    # Strip leading code fence.
    if text.startswith("```"):
        # Drop the first line.
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"herald output was not a JSON object (got {type(data).__name__})")
    if "yesterday" not in data or "priorities" not in data:
        raise ValueError("herald output missing required fields (yesterday, priorities)")
    return data


def draft_from_herald_json(data: dict, *, date_local: str) -> BriefingDraft:
    priorities_raw = data.get("priorities") or []
    priorities: list[PriorityLine] = []
    for item in priorities_raw:
        if not isinstance(item, dict):
            continue
        priorities.append(
            PriorityLine(
                title=str(item.get("title") or ""),
                project=str(item.get("project") or ""),
                why=str(item.get("why") or ""),
            )
        )

    watch_raw = data.get("watch") or []
    watch: list[str] = []
    if isinstance(watch_raw, list):
        watch = [str(w) for w in watch_raw if isinstance(w, (str, int))][:3]

    yesterday_text = str(data.get("yesterday") or "").strip()
    markdown = _render_synthesized_markdown(yesterday_text, priorities, watch)
    return BriefingDraft(
        date_local=date_local,
        mode="synthesized",
        yesterday=yesterday_text,
        priorities=priorities,
        watch=watch,
        markdown=markdown,
    )


def _render_synthesized_markdown(
    yesterday_text: str,
    priorities: list[PriorityLine],
    watch: list[str],
) -> str:
    lines: list[str] = []
    lines.append("## Yesterday")
    lines.append(yesterday_text or "_(no narrative)_")
    lines.append("")
    lines.append("## Today's priorities")
    if not priorities:
        lines.append("- _(nothing queued)_")
    else:
        for p in priorities[:5]:
            bullet = f"- **{p.project}**: {p.title}"
            if p.why:
                bullet += f" — {p.why}"
            lines.append(bullet)
    if watch:
        lines.append("")
        lines.append("## Watch")
        for w in watch[:2]:
            lines.append(f"- {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fallback briefing
# ---------------------------------------------------------------------------


_FALLBACK_PREAMBLE = (
    "> _(generated without synthesis — the herald session was unavailable. "
    "Check logs for details.)_"
)


def build_fallback_draft(
    *,
    snapshot: YesterdaySnapshot,
    priorities: PriorityList,
    date_local: str,
    reason: str = "",
) -> BriefingDraft:
    """Assemble a structured briefing directly from the data."""
    completed = [
        t for t in snapshot.task_transitions if t.to_state in {"done", "completed"}
    ]
    approved = [
        t for t in snapshot.task_transitions
        if t.to_state in {"approved"} or (t.from_state == "review" and t.to_state == "done")
    ]
    projects_with_commits = sum(
        1 for commits in snapshot.commits_by_project.values() if commits
    )

    yesterday_text = (
        f"Yesterday: {snapshot.total_commits()} commits across "
        f"{projects_with_commits} project(s), "
        f"{len(completed)} tasks completed, {len(approved)} approvals, "
        f"{len(snapshot.advisor_insights)} advisor insights, "
        f"{len(snapshot.downtime_artifacts)} downtime artifacts awaiting approval."
    )

    priority_lines: list[PriorityLine] = [
        PriorityLine(
            title=p.title,
            project=p.project,
            why=f"[{p.priority}] {p.state}, stale {p.age_seconds / 3600:.1f}h",
        )
        for p in priorities.top_tasks
    ]

    watch: list[str] = []
    for b in priorities.blockers[:2]:
        if b.unresolved_blockers:
            watch.append(
                f"Blocked: {b.task_id} ({b.title}) — blocked by "
                f"{', '.join(b.unresolved_blockers)}"
            )
    for a in priorities.awaiting_approval[:2]:
        watch.append(
            f"Aging approval ({a.age_hours:.0f}h): {a.subject} ({a.kind})"
        )

    md_lines = [_FALLBACK_PREAMBLE, ""]
    md_lines.append("## Yesterday")
    md_lines.append(yesterday_text)
    md_lines.append("")
    md_lines.append("## Today's priorities")
    if not priority_lines:
        md_lines.append("- _(nothing queued)_")
    else:
        for p in priority_lines[:5]:
            md_lines.append(f"- **{p.project}**: {p.title} — {p.why}")
    if watch:
        md_lines.append("")
        md_lines.append("## Watch")
        for w in watch[:2]:
            md_lines.append(f"- {w}")

    return BriefingDraft(
        date_local=date_local,
        mode="fallback",
        yesterday=yesterday_text,
        priorities=priority_lines,
        watch=watch[:2],
        markdown="\n".join(md_lines),
        meta={"fallback_reason": reason} if reason else {},
    )


# ---------------------------------------------------------------------------
# Quiet mode detection
# ---------------------------------------------------------------------------


def _snapshot_is_quiet(snapshot: YesterdaySnapshot) -> bool:
    """True when the snapshot has no activity signals."""
    if snapshot.total_commits() > 0:
        return False
    if snapshot.task_transitions:
        return False
    if snapshot.advisor_insights:
        return False
    if snapshot.downtime_artifacts:
        return False
    return True


def detect_quiet_mode(
    config,
    *,
    now_local: datetime,
    quiet_threshold_days: int,
    project_root: Path | None = None,
    gather_func: Callable = gather_yesterday,
) -> bool:
    """Return True when the last ``quiet_threshold_days`` were all silent.

    Runs one ``gather_yesterday`` probe per past day. Cheap — each probe
    is a scoped SQL + git log per project. For the default 7-day
    threshold across a 10-project setup this is well under 2 seconds.
    """
    if quiet_threshold_days < 1:
        return False
    for offset in range(1, quiet_threshold_days + 1):
        probe_now = now_local - timedelta(days=offset - 1)
        snapshot = gather_func(
            config, now_local=probe_now, project_root=project_root,
        )
        if not _snapshot_is_quiet(snapshot):
            return False
    return True


def is_weekly_quiet_fire_day(now_local: datetime) -> bool:
    """Return True on Sundays (local). Used as the quiet-mode cadence gate."""
    # Monday=0 .. Sunday=6 in Python's weekday().
    return now_local.weekday() == 6


# ---------------------------------------------------------------------------
# High-level entrypoint
# ---------------------------------------------------------------------------


def synthesize_briefing(
    *,
    config,
    snapshot: YesterdaySnapshot,
    priorities: PriorityList,
    base_dir: Path,
    date_local: str,
    budget_seconds: int = 300,
) -> BriefingDraft:
    """Produce a briefing draft — herald synthesis preferred, fallback on error.

    Does not write the inbox; mb04 owns that. Does write:

    * The context pack as ``<base_dir>/last-briefing-context.md`` (for
      audit — lets the operator see exactly what the herald saw).
    * The briefing log entry on success or fallback.
    """
    recent = load_recent_briefings(base_dir, limit=3)
    context_md = build_context_md(
        snapshot=snapshot, priorities=priorities, recent=recent,
    )
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_text(Path(base_dir) / "last-briefing-context.md", context_md)
    except OSError as exc:
        logger.debug("briefing: failed to write context pack: %s", exc)

    draft: BriefingDraft
    try:
        raw = herald_invocation(context_md, budget_seconds=budget_seconds)
        data = parse_herald_output(raw)
        draft = draft_from_herald_json(data, date_local=date_local)
    except NotImplementedError as exc:
        logger.info("briefing: herald invocation not installed; using fallback")
        draft = build_fallback_draft(
            snapshot=snapshot, priorities=priorities,
            date_local=date_local, reason=f"no-herald-installed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: herald synthesis failed: %s", exc)
        draft = build_fallback_draft(
            snapshot=snapshot, priorities=priorities,
            date_local=date_local, reason=f"{type(exc).__name__}: {exc}",
        )

    # Append to the briefing log regardless of mode.
    try:
        append_briefing_log(
            base_dir,
            {
                "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
                "date_local": draft.date_local,
                "mode": draft.mode,
                "yesterday": draft.yesterday,
                "priorities": [asdict(p) for p in draft.priorities],
                "watch": list(draft.watch),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("briefing: failed to append briefing log: %s", exc)

    return draft


def build_quiet_mode_draft(*, date_local: str) -> BriefingDraft:
    """One-line 'quiet' briefing emitted during vacation mode."""
    body = (
        "## Quiet mode\n"
        "No activity for the last stretch of days — downshifting to weekly.\n"
        "I'll check in again next Sunday. Reply `/briefing enable` to resume dailies."
    )
    return BriefingDraft(
        date_local=date_local,
        mode="quiet-mode",
        yesterday="No activity; quiet mode active.",
        markdown=body,
    )
