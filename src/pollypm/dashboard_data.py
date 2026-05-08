"""Gather dashboard data from git, issues, snapshots, and state."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import load_config
from pollypm.config import PollyPMConfig
from pollypm.storage.state import StateStore


# ANSI CSI/OSC escapes plus C0 control chars that can survive in a
# tmux pane snapshot. ``readyring`` and friends in the cockpit Now
# panel (#792) are produced when an in-flight render's overlapping
# fragments make it into the snapshot text — strip the control bytes
# before parsing the snapshot.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07?)")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOX_DRAWING_PREFIXES = (
    "─",
    "│",
    "┌",
    "┐",
    "└",
    "┘",
    "├",
    "┤",
    "┬",
    "┴",
    "┼",
    "╭",
    "╮",
    "╰",
    "╯",
)


def _sanitize_snapshot_line(text: str) -> tuple[str, bool]:
    """Strip ANSI/control bytes from a snapshot line.

    Returns ``(cleaned_text, was_dirty)``. ``was_dirty`` is True when
    the source line carried ANSI escapes or control chars — those
    snapshots come from in-flight renders where adjacent fragments
    can fuse on strip (``ready\x1b[Kring`` → ``readyring``), so the
    caller should treat such lines as untrustworthy.
    """
    had_escape = bool(_ANSI_ESCAPE_RE.search(text) or _CONTROL_CHARS_RE.search(text))
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, had_escape


def _truncate_for_now_panel(text: str, *, limit: int = 70) -> str:
    """Truncate snapshot text at a word boundary, append ``…`` (#792).

    The Now panel previously sliced ``[:70]`` directly, so a status
    like ``"… is on hold awaiting your Phase A decision."`` rendered
    as ``"… is on hold awaiting your Phase A decisio"`` — chopped
    mid-word with no ellipsis to signal the cut.
    """
    if len(text) <= limit:
        return text
    # Look back from the limit for the last space; fall back to a hard
    # cut if the word itself is longer than the budget.
    cut = text.rfind(" ", 0, limit)
    if cut <= 0 or cut < limit - 20:
        cut = limit - 1
    return text[:cut].rstrip() + "…"


_NO_TASKS_FOUND_RE = re.compile(
    r"(?:\bpm\s+task\s+list\b.*)?\b(?:result:\s*)?no tasks found\b",
    re.IGNORECASE,
)
_ZERO_DURATION_RE = re.compile(r"0[sm](?:\s*0s)?")


def _is_zero_duration(text: str) -> bool:
    return _ZERO_DURATION_RE.fullmatch(text.strip()) is not None


def _now_feed_friendly_fallback(line: str) -> str | None:
    """Return cleaner Home Now copy for raw or fragmentary pane lines."""
    text = line.strip()
    if not text:
        return None
    lower = text.lower()
    if _NO_TASKS_FOUND_RE.search(text):
        return "Nothing on the burner"
    if "implementing worker tasks" in lower:
        return "On the line"
    if text[0].islower():
        return "On the line"
    return None


# Codex idle-input placeholder detection lives in
# :mod:`pollypm.idle_placeholders` (#1010 extraction) so the dashboard
# renderer here and the heartbeat session-health classifier share one
# definition. Re-export under the legacy name for backward compat with
# any external callers / tests pinning the old symbol.
from pollypm.idle_placeholders import (
    CODEX_IDLE_PLACEHOLDERS as _CODEX_IDLE_PLACEHOLDERS,
    is_codex_idle_placeholder as _is_codex_idle_placeholder,
)


def _snapshot_activity_status(line: str) -> str | None:
    """Summarize Claude/Codex status chrome instead of echoing it verbatim."""
    match = re.search(
        r"\((?P<age>\d+[smh](?:\s*\d+s)?)\s*·[^)]*\btokens?\b(?P<tail>[^)]*)\)",
        line,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    age = match.group("age").strip()
    tail = match.group("tail").lower()
    if "thinking" in tail:
        state = "thinking"
    else:
        state = "active"
    return f"{state} ({age})"


@dataclass(slots=True)
class CommitInfo:
    hash: str
    message: str
    author: str
    age_seconds: float
    project: str


@dataclass(slots=True)
class SessionActivity:
    name: str
    role: str
    project: str
    project_label: str
    status: str
    description: str  # human-readable "what it's doing"
    age_seconds: float


@dataclass(slots=True)
class CompletedItem:
    title: str
    kind: str  # "issue", "commit", "pr"
    project: str
    age_seconds: float


@dataclass(slots=True)
class InboxPreview:
    sender: str
    title: str
    project: str
    task_id: str
    age_seconds: float


@dataclass(slots=True)
class AccountQuotaUsage:
    account_name: str
    provider: str
    email: str
    used_pct: int
    summary: str
    severity: str
    limit_label: str = "limit"
    reset_at: str = ""


@dataclass(slots=True)
class DashboardData:
    active_sessions: list[SessionActivity]
    recent_commits: list[CommitInfo]
    completed_items: list[CompletedItem]
    recent_messages: list[InboxPreview]
    daily_tokens: list[tuple[str, int]]  # (date, tokens)
    today_tokens: int
    total_tokens: int
    sweep_count_24h: int
    message_count_24h: int
    recovery_count_24h: int
    inbox_count: int
    alert_count: int
    account_usages: list[AccountQuotaUsage] = field(default_factory=list)
    briefing: str = ""  # morning briefing narrative (if user was away)


# Per-project git-log cache: ``(project_path, hours) -> (cached_at, rows)``.
# The polly-dashboard refreshes every 10s and this helper used to spawn
# one ``git log`` subprocess per project per refresh — at 9 projects ×
# (up to) 5s timeout that was 9 forks every tick and a 45s worst-case
# hang on a single slow repo. Cache for 60s: dashboard ticks 6x within
# the window pay zero subprocess cost. Cache key is the project path so
# a config edit that drops a project just stops looking it up.
_COMMIT_CACHE: dict[tuple[str, int], tuple[float, list["_CachedCommitRow"]]] = {}
_COMMIT_CACHE_TTL_SECONDS = 60.0
_COMMIT_PER_PROJECT_TIMEOUT_SECONDS = 2.0


from dataclasses import dataclass as _dataclass


@_dataclass(slots=True, frozen=True)
class _CachedCommitRow:
    """git-log row stored in the cache — converted to CommitInfo on read.

    Stored separately so the cached ``age_seconds`` doesn't drift; the
    consumer recomputes age from ``date_iso`` at the moment of read.
    """
    hash7: str
    message: str
    author: str
    date_iso: str


def _git_log_rows_cached(project_path: Path, hours: int) -> list[_CachedCommitRow]:
    """Return cached git-log rows for ``project_path``, refreshing on TTL."""
    import time as _time

    cache_key = (str(project_path), hours)
    cached = _COMMIT_CACHE.get(cache_key)
    now_mono = _time.monotonic()
    if cached is not None and (now_mono - cached[0]) < _COMMIT_CACHE_TTL_SECONDS:
        return cached[1]

    git_dir = project_path / ".git"
    if not git_dir.exists():
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []
    try:
        result = subprocess.run(
            ["git", "log", f"--since={hours} hours ago", "--format=%H\t%s\t%an\t%aI", "--all"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=_COMMIT_PER_PROJECT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Cache the empty result too — a slow repo shouldn't be retried
        # every refresh tick. The TTL still applies, so the next 60s of
        # ticks return [] instantly instead of re-spawning git.
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []
    if result.returncode != 0:
        _COMMIT_CACHE[cache_key] = (now_mono, [])
        return []

    rows: list[_CachedCommitRow] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        h, msg, author, date_str = parts
        rows.append(
            _CachedCommitRow(hash7=h[:7], message=msg[:80], author=author, date_iso=date_str)
        )
    _COMMIT_CACHE[cache_key] = (now_mono, rows)
    return rows


def _recent_commits(config: PollyPMConfig, hours: int = 24) -> list[CommitInfo]:
    """Get git commits from the last N hours across all projects.

    Backed by a per-project ``git log`` cache (60s TTL) so the
    dashboard's 10s refresh tick doesn't re-spawn a subprocess per
    project on every tick.
    """
    commits: list[CommitInfo] = []
    now = datetime.now(UTC)
    seen: set[str] = set()

    for key, project in config.projects.items():
        for row in _git_log_rows_cached(project.path, hours):
            if row.hash7 in seen:
                continue
            seen.add(row.hash7)
            try:
                age = (now - datetime.fromisoformat(row.date_iso)).total_seconds()
            except (ValueError, TypeError):
                age = 0.0
            commits.append(CommitInfo(
                hash=row.hash7,
                message=row.message,
                author=row.author,
                age_seconds=age,
                project=key,
            ))

    commits.sort(key=lambda c: c.age_seconds)
    return commits


def _completed_issues(config: PollyPMConfig, hours: int = 72) -> list[CompletedItem]:
    """Find recently completed issues across projects."""
    items: list[CompletedItem] = []
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)

    for key, project in config.projects.items():
        completed_dir = project.path / "issues" / "05-completed"
        if not completed_dir.exists():
            continue
        for f in sorted(completed_dir.glob("*.md"), reverse=True):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    continue
                # Extract title from filename: 0035-some-title.md -> some title
                stem = f.stem
                parts = stem.split("-", 1)
                title = parts[1].replace("-", " ") if len(parts) > 1 else stem
                items.append(CompletedItem(
                    title=title, kind="issue", project=key,
                    age_seconds=(now - mtime).total_seconds(),
                ))
            except (OSError, ValueError):
                continue

    items.sort(key=lambda i: i.age_seconds)
    return items[:10]


# #1025 — recognized event signatures the Home "Now" feed should
# prefer over whatever the agent's cursor happens to land on. Patterns
# are checked against the trailing slice of the pane snapshot
# (most-recent activity wins). Each pattern matches a line that is
# safe to echo verbatim into the panel.
_NOW_EVENT_SIGNATURES: tuple[re.Pattern[str], ...] = (
    # Test summaries — pytest, jest, mocha, go test, cargo test, etc.
    re.compile(r"\b\d+\s+(?:passed|failed|errored?|skipped)\b", re.IGNORECASE),
    re.compile(r"\bTests?:\s+\d+", re.IGNORECASE),
    re.compile(r"\bok\s+\d+\s+tests?\b", re.IGNORECASE),
    # Commit / push events.
    re.compile(r"^\s*\[[^\]]+\s+[0-9a-f]{7,}\]"),  # `[main abc1234] msg`
    re.compile(r"\bCommitted\b", re.IGNORECASE),
    re.compile(r"\b\d+ files? changed", re.IGNORECASE),
    re.compile(r"\bTo (?:github\.com|gitlab\.com|bitbucket\.org|git@)"),
    # Build / lint / type check completion.
    re.compile(r"\bBuild (?:succeeded|failed|complete)\b", re.IGNORECASE),
    re.compile(r"\bcompiled successfully\b", re.IGNORECASE),
    re.compile(r"\bno (?:errors|issues)\b", re.IGNORECASE),
    # Status / state transitions surfaced by the agent.
    re.compile(r"\bStatus:\s+", re.IGNORECASE),
    re.compile(r"\b(?:queued|in_progress|review|done|blocked|on_hold)\s+→\s+"),
    # Alerts / warnings the agent emitted.
    re.compile(r"^\s*(?:Alert|Warning|ERROR):\s+", re.IGNORECASE),
)


_UPSTREAM_CLI_TIP_SUBSTRINGS = (
    "try the codex app",
    "visit https://chatgpt.com",
)
_UPSTREAM_CLI_TIP_COMMAND_RE = re.compile(r"^(?:new\s+)?use\s+/[a-z]", re.IGNORECASE)


def _is_upstream_cli_tip(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    lower = text.lower()
    return (
        lower.startswith("tip:")
        or _UPSTREAM_CLI_TIP_COMMAND_RE.search(text) is not None
        or any(marker in lower for marker in _UPSTREAM_CLI_TIP_SUBSTRINGS)
    )


def _scan_for_event_signature(clean_lines: list[str]) -> str | None:
    """Return the most-recent line matching a recognized event
    signature, or None if no such line exists in the trailing window.

    The Home dashboard's "Now" feed used to fall back to
    last-line-of-pane when no progress indicator matched. Last-line is
    routinely mid-sentence noise (the cursor position when capture
    ran), while a real event ("312 passed in 24.80s", "Committed
    abc1234", "Status: review") that the agent emitted three lines
    back is the user-meaningful signal. Scan the bottom half of the
    pane (the most-recent activity) for any of the recognized event
    patterns; return the latest match.
    """
    if not clean_lines:
        return None
    # Look at the last ~40 lines — wide enough to catch a multi-line
    # commit / test-summary block, narrow enough to stay biased to
    # recent activity. The "Now" feed is about *current* state, not
    # archaeological reconstruction.
    window = clean_lines[-40:]
    for line in reversed(window):
        stripped = line.strip()
        if not stripped or len(stripped) < 5:
            continue
        # Skip lines that the existing fall-through filter would
        # already drop — same prompt prefixes, same TUI chrome.
        if stripped.startswith(("❯", "›", ">", "$", "%", *_BOX_DRAWING_PREFIXES)):
            continue
        if _is_upstream_cli_tip(stripped):
            continue
        for pattern in _NOW_EVENT_SIGNATURES:
            if pattern.search(stripped):
                return stripped
    return None


def _session_description(status: str, role: str, snapshot_path: str | None) -> str:
    """Build a human-readable description of what a session is doing."""
    if role == "operator-pm":
        if status == "healthy":
            return "Plating the brief"
        if status == "waiting_on_user":
            return "waiting for your direction"
        return "supervising"
    if role == "heartbeat-supervisor":
        return "monitoring all sessions"
    # Worker — try to get context from the last snapshot
    if snapshot_path:
        try:
            text = Path(snapshot_path).read_text(errors="ignore")
            # Strip ANSI escapes and control bytes up front. Without
            # this, in-flight Claude renders leak overlapping
            # fragments like ``ready\x1b[Kring…`` that read as
            # ``readyring`` in the panel (#792). When a line had
            # escapes, the cleaned text is unreliable (adjacent
            # fragments may have fused), so we mark those lines and
            # only use them as a last-resort fallback.
            sanitized = [
                _sanitize_snapshot_line(line)
                for line in text.splitlines()
            ]
            cleaned_lines = [text for text, _dirty in sanitized]
            clean_lines = [text for text, dirty in sanitized if not dirty]
            # Check for progress indicators first — only over the
            # trustworthy (escape-free) lines so we don't echo a
            # half-rendered status string.
            for stripped in clean_lines:
                # pytest: "312 passed in 24.80s" or "collecting ..."
                if re.search(r"\d+ passed", stripped):
                    return _truncate_for_now_panel(stripped)
                # npm/build progress
                if "building" in stripped.lower() and ("%" in stripped or "/" in stripped):
                    return _truncate_for_now_panel(stripped)
                # Working indicator with time
                m = re.search(r"Working \((\d+[ms]\s?\d*s?)\s*", stripped)
                if m:
                    duration = m.group(1).strip()
                    if _is_zero_duration(duration):
                        return "Warming up"
                    return f"working ({duration})"
            # #1025 (Home dashboard "Now" feed) — when the visible
            # last-line of pane is mid-sentence noise, prefer a
            # recognized event signature picked from the most-recent
            # half of the pane. This catches "X failed", "Committed",
            # "Status: …", and "Alert:" lines that an agent emitted a
            # few lines back but which are far more meaningful than
            # whatever the cursor happens to be sitting on right now.
            event_match = _scan_for_event_signature(clean_lines)
            if event_match:
                friendly = _now_feed_friendly_fallback(event_match)
                if friendly is not None:
                    return friendly
                return _truncate_for_now_panel(event_match)
            # Look for meaningful lines in the snapshot — restrict to
            # escape-free lines so a fused render (#792) doesn't end
            # up as the displayed status.
            for line in reversed(clean_lines):
                if not line or len(line) < 10:
                    continue
                # Skip prompt lines and noise. ``›`` (U+203A) is the
                # Codex CLI's idle prompt arrow; when Codex is sitting
                # at an empty input box it renders rotating placeholder
                # hints prefixed with ``›`` ("› Run /review on my
                # current changes", "› Explain this codebase", etc).
                # Those are NOT the agent's activity — they're the
                # CLI's grey suggestion text — so treat the line the
                # same as Claude's ``❯`` idle prompt and fall through
                # to the status-based default (#994).
                if line.startswith(("❯", "›", ">", "$", "%", *_BOX_DRAWING_PREFIXES)):
                    continue
                if "gpt-" in line.lower() or "default ·" in line:
                    continue
                # Claude TUI bottom-bar boilerplate. ``⏵⏵`` is the
                # bypass-permissions hint; the others are standing
                # keybinding cues that appear on every snapshot when
                # the session is idle at the prompt. Reporting them
                # as "what's happening now" is misleading — the
                # session isn't *doing* the bypass-permissions thing,
                # it's idle waiting for input.
                lower = line.lower()
                activity_status = _snapshot_activity_status(line)
                if activity_status is not None:
                    return activity_status
                if "readyring" in lower or lower.startswith("readying"):
                    continue
                if (
                    "bypass permissions on" in lower
                    or "ctrl+t to hide tasks" in lower
                    or "ctrl+t to show tasks" in lower
                    or "shift+tab to cycle" in lower
                    or line.startswith("⏵⏵")
                ):
                    continue
                if _is_upstream_cli_tip(line):
                    continue
                # Codex idle-input placeholder hints — defensive net in
                # case the ``›`` prompt arrow gets stripped during pane
                # capture but the suggestion text survives. These are
                # the rotating greys that Codex shows in an empty input
                # box (#994).
                if _is_codex_idle_placeholder(line):
                    continue
                friendly = _now_feed_friendly_fallback(line)
                if friendly is not None:
                    return friendly
                return _truncate_for_now_panel(line)
        except (FileNotFoundError, OSError):
            pass
    if status == "waiting_on_user":
        return "waiting for your input"
    if status == "healthy":
        # Use ``idle`` instead of ``working`` — the rail spinner
        # activates on any label ending in ``working``, so mapping
        # the catchall healthy case to ``working`` made Polly's
        # spinner spin forever whenever she wasn't mid-turn (2026-04-20
        # desktop screenshot). ``idle`` reads better in the UI and
        # correctly pauses the spinner until Claude Code itself
        # reports a ``Working (Nm)`` line in the pane snapshot
        # (detected above).
        return "idle"
    if status == "needs_followup":
        return "in progress"
    return status


def _count_inbox_tasks(config: PollyPMConfig) -> int:
    """Total inbox tasks across all tracked projects (work-service backed)."""
    try:
        from pollypm.work import create_work_service
        from pollypm.work.inbox_view import inbox_tasks
    except Exception:  # noqa: BLE001
        return 0
    total = 0
    for project_key, project in getattr(config, "projects", {}).items():
        # Same invariant as recovery_prompt._pending_inbox_section
        # (cycle 85) and the doctor's sweeper-dbs check: only tracked
        # projects' state.db files are PollyPM-owned. A registered-
        # but-not-tracked project may have a stale .pollypm/state.db
        # left over from a prior tracking run; counting its leftover
        # inbox tasks inflates the morning-briefing count and the
        # doctor's "open inbox items" check.
        if not getattr(project, "tracked", False):
            continue
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            with create_work_service(
                db_path=db_path, project_path=project.path,
            ) as svc:
                total += len(inbox_tasks(svc, project=project_key))
        except Exception:  # noqa: BLE001
            continue
    return total


def _count_dashboard_inbox_items(config: PollyPMConfig) -> int:
    """Return the user-facing inbox count shown on the cockpit home.

    The cockpit home and rail are the same at-a-glance surface, so their
    inbox counts must come from the same registered-project scan. The
    older tracked-only helper remains for doctor / recovery checks that
    intentionally ignore untracked project DBs.
    """
    try:
        from pollypm.cockpit_inbox import _count_inbox_tasks_for_label
        return int(_count_inbox_tasks_for_label(config) or 0)
    except Exception:  # noqa: BLE001
        return _count_inbox_tasks(config)


def _user_waiting_task_ids_across_projects(
    config: PollyPMConfig,
) -> frozenset[str]:
    """Return ``project/N`` ids for every task in a user-waiting state
    across every tracked project.

    Used to suppress ``stuck_on_task:<id>`` alerts that are already
    covered by the project's user-waiting status.
    """
    from pollypm.work.task_state import user_waiting_task_ids

    return user_waiting_task_ids(config)


def _stuck_alert_already_user_waiting(
    alert_type: str, user_waiting_task_ids: frozenset[str],
) -> bool:
    """Return True for ``stuck_on_task:<id>`` alerts on a user-
    waiting task. Mirror of the rail-side helper in
    ``cockpit_rail._stuck_alert_already_user_waiting``.
    """
    prefix = "stuck_on_task:"
    if not alert_type or not alert_type.startswith(prefix):
        return False
    task_id = alert_type[len(prefix):].strip()
    return bool(task_id) and task_id in user_waiting_task_ids


def _inbox_sender(task) -> str:
    roles = getattr(task, "roles", {}) or {}
    operator = roles.get("operator")
    if operator and operator != "user":
        return str(operator)
    created_by = getattr(task, "created_by", "")
    if created_by and created_by != "user":
        return str(created_by)
    return "polly"


def _recent_inbox_messages(config: PollyPMConfig, *, limit: int = 3) -> list[InboxPreview]:
    try:
        from pollypm.work import create_work_service
        from pollypm.work.inbox_view import inbox_tasks
    except Exception:  # noqa: BLE001
        return []

    now = datetime.now(UTC)
    seen_task_ids: set[str] = set()
    previews: list[InboxPreview] = []
    sources: list[tuple[str | None, str, Path, Path]] = []
    for project_key, project in getattr(config, "projects", {}).items():
        # Same tracked-only invariant as _count_inbox_tasks (cycle 86):
        # a non-tracked project's leftover state.db would leak stale
        # tasks into the polly-dashboard's "Recent messages" preview.
        if not getattr(project, "tracked", False):
            continue
        sources.append((project_key, project.display_label(), project.path / ".pollypm" / "state.db", project.path))
    workspace_root = getattr(getattr(config, "project", None), "workspace_root", None)
    if workspace_root is not None:
        workspace_path = Path(workspace_root)
        sources.append((None, "Workspace", workspace_path / ".pollypm" / "state.db", workspace_path))

    for project_key, project_label, db_path, project_path in sources:
        if not db_path.exists():
            continue
        try:
            with create_work_service(db_path=db_path, project_path=project_path) as svc:
                for task in inbox_tasks(svc, project=project_key):
                    if task.task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task.task_id)
                    stamped = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
                    if hasattr(stamped, "timestamp"):
                        age_seconds = max(0.0, now.timestamp() - float(stamped.timestamp()))
                    else:
                        try:
                            age_seconds = max(
                                0.0,
                                (now - datetime.fromisoformat(str(stamped))).total_seconds(),
                            )
                        except (ValueError, TypeError):
                            age_seconds = 0.0
                    previews.append(
                        InboxPreview(
                            sender=_inbox_sender(task),
                            title=(getattr(task, "title", "") or "(untitled)")[:80],
                            project=project_label,
                            task_id=task.task_id,
                            age_seconds=age_seconds,
                        )
                    )
        except Exception:  # noqa: BLE001
            continue

    previews.sort(key=lambda item: item.age_seconds)
    return previews[:limit]


def _provider_label(provider: object) -> str:
    raw = getattr(provider, "value", provider)
    text = str(raw or "").strip().lower()
    if text in {"claude", "anthropic"}:
        return "Anthropic"
    if text == "codex":
        return "OpenAI"
    if not text:
        return ""
    return text.capitalize()


def _quota_severity(used_pct: int) -> str:
    if used_pct >= 95:
        return "critical"
    if used_pct >= 80:
        return "warning"
    return "ok"


def _quota_limit_label(period_label: object) -> str:
    label = str(period_label or "").strip().lower()
    if "week" in label:
        return "weekly limit"
    if "month" in label:
        return "monthly limit"
    if "day" in label:
        return "daily limit"
    return "limit"


def _account_quota_usage(config: PollyPMConfig, store: StateStore) -> list[AccountQuotaUsage]:
    """Return cached LLM account quota percentages for the Home dashboard."""
    rows: list[AccountQuotaUsage] = []
    seen_emails: set[str] = set()
    for account_name, account in getattr(config, "accounts", {}).items():
        email = (getattr(account, "email", None) or "").strip()
        if email:
            if email in seen_emails:
                continue
            seen_emails.add(email)
        try:
            usage = store.get_account_usage(account_name)
        except Exception:  # noqa: BLE001
            usage = None
        used_pct = getattr(usage, "used_pct", None) if usage is not None else None
        if used_pct is None:
            continue
        pct = int(used_pct)
        rows.append(
            AccountQuotaUsage(
                account_name=account_name,
                provider=_provider_label(
                    getattr(account, "provider", None)
                    or getattr(usage, "provider", "")
                ),
                email=email,
                used_pct=pct,
                summary=str(getattr(usage, "usage_summary", "") or f"{pct}% used"),
                severity=_quota_severity(pct),
                limit_label=_quota_limit_label(getattr(usage, "period_label", "")),
                reset_at=str(getattr(usage, "reset_at", "") or ""),
            )
        )
    rows.sort(
        key=lambda row: (
            -row.used_pct,
            row.provider.lower(),
            row.account_name,
        )
    )
    return rows


def load_dashboard(config_path: Path) -> tuple[PollyPMConfig, DashboardData]:
    """Load config + state store and gather one blocking dashboard snapshot."""
    config = load_config(config_path)
    store = StateStore(config.project.state_db)
    try:
        data = gather(config, store)
    finally:
        store.close()
    return config, data


def gather(config: PollyPMConfig, store: StateStore) -> DashboardData:
    """Gather all dashboard data."""
    from pollypm.service_api import plan_launches_readonly

    now = datetime.now(UTC)

    # Active sessions
    all_runtimes = store.list_session_runtimes()
    runtime_map = {rt.session_name: rt for rt in all_runtimes}
    launches = plan_launches_readonly(config, store)

    active: list[SessionActivity] = []
    for launch in launches:
        rt = runtime_map.get(launch.session.name)
        status = rt.status if rt else "unknown"
        project = config.projects.get(launch.session.project)
        label = project.display_label() if project else launch.session.project

        # Get last snapshot path for description
        hb = store.latest_heartbeat(launch.session.name)
        snapshot_path = hb.snapshot_path if hb else None

        desc = _session_description(status, launch.session.role, snapshot_path)
        age = 0.0
        if rt and rt.updated_at:
            try:
                age = (now - datetime.fromisoformat(rt.updated_at)).total_seconds()
            except (ValueError, TypeError):
                pass

        active.append(SessionActivity(
            name=launch.session.name, role=launch.session.role,
            project=launch.session.project, project_label=label,
            status=status, description=desc, age_seconds=age,
        ))

    # Events summary
    recent = store.recent_events(limit=300)
    cutoff = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff]

    # Token data
    daily = store.daily_token_usage(days=30)
    values = [t for _, t in daily]
    today_str = now.strftime("%Y-%m-%d")
    today_tokens = next((t for d, t in daily if d == today_str), 0)
    account_usages = _account_quota_usage(config, store)

    commits = _recent_commits(config, hours=24)
    completed = _completed_issues(config, hours=72)
    inbox_count = _count_dashboard_inbox_items(config)
    recent_messages = _recent_inbox_messages(config)
    sweeps = sum(1 for e in day_events if e.event_type == "heartbeat")
    recoveries = sum(1 for e in day_events if "recover" in e.event_type)

    # Morning briefing: generate if there are overnight results
    def _plural(count: int, singular: str, plural: str | None = None) -> str:
        word = singular if count == 1 else (plural or f"{singular}s")
        return f"{count} {word}"

    # 24h activity briefing. The earlier "While you were away" framing
    # presumed every cockpit launch was a return from a trip — including
    # the literal first-ever launch on a fresh install (#854). Use a
    # neutral 24-hour heading instead. Recovery counts are internal
    # plumbing (see #879 for elevation policy) and are deliberately not
    # surfaced here so the dashboard reflects what the user did, not
    # what the supervisor patched up behind the scenes.
    briefing = ""
    if commits or completed or inbox_count:
        parts: list[str] = []
        if commits:
            projects_touched = len({c.project for c in commits})
            parts.append(
                f"{_plural(len(commits), 'commit')} across "
                f"{_plural(projects_touched, 'project')}"
            )
        if completed:
            parts.append(f"{_plural(len(completed), 'issue')} completed")
        if inbox_count:
            parts.append(
                f"{_plural(inbox_count, 'inbox item')} waiting for you"
            )
        briefing = "Last 24 hours: " + ", ".join(parts) + "."

    # Late import keeps dashboard_data out of the cockpit_alerts import
    # graph (cockpit_alerts → cockpit_palette → cockpit, which pulls
    # dashboard_data in at top level).
    from pollypm.cockpit_alerts import is_operational_alert

    # Drop ``stuck_on_task:<id>`` alerts whose task is already in a
    # user-waiting state — the session sat idle because the user
    # hasn't responded, which is the system doing what it should,
    # not a fault to surface as a separate alert. Mirrors cycles 45
    # / 53 / 55 dedup at the global polly-dashboard count level.
    user_waiting = _user_waiting_task_ids_across_projects(config)
    alert_count = sum(
        1 for a in store.open_alerts()
        if not is_operational_alert(a.alert_type)
        and not _stuck_alert_already_user_waiting(
            a.alert_type, user_waiting,
        )
    )

    return DashboardData(
        active_sessions=active,
        recent_commits=commits,
        completed_items=completed,
        recent_messages=recent_messages,
        daily_tokens=daily,
        today_tokens=today_tokens,
        total_tokens=sum(values),
        sweep_count_24h=sweeps,
        message_count_24h=sum(1 for e in day_events if e.event_type == "send_input"),
        recovery_count_24h=recoveries,
        inbox_count=inbox_count,
        alert_count=alert_count,
        account_usages=account_usages,
        briefing=briefing,
    )
