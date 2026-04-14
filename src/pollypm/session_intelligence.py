"""Session Intelligence — unified Haiku call per session.

Two modes of operation:

1. **Triage** (60s, stalled sessions only): Called from heartbeat sweep
   when a worker is idle. Returns action (proceed/nudge/idle) + knowledge.

2. **Sweep** (5 min, all sessions with activity): Processes every session
   that has new transcript events since last cursor. Extracts knowledge
   and activity summaries. Does NOT triage (that's the 60s path).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.atomic_io import atomic_write_text
from pollypm.llm_runner import run_haiku_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionIntelligenceResult:
    action: str = "idle"  # "proceed" | "nudge" | "idle"
    action_message: str = ""
    knowledge_entries: list[dict] = field(default_factory=list)
    activity_summary: str = ""
    has_work_product: bool = False


def run_session_intelligence(
    snapshot: str,
    transcript_delta: str,
    session_name: str,
    role: str,
    has_pending_work: bool,
) -> SessionIntelligenceResult | None:
    """Single Haiku call per session. Returns triage + knowledge + summary.

    Returns None on failure (caller should fall back to heuristics).
    """
    if not snapshot and not transcript_delta:
        return None

    prompt = _build_prompt(snapshot, transcript_delta, session_name, role, has_pending_work)
    result = run_haiku_json(prompt)
    if result is None:
        return None

    return _parse_result(result)


def _build_prompt(
    snapshot: str,
    transcript_delta: str,
    session_name: str,
    role: str,
    has_pending_work: bool,
) -> str:
    parts = [
        f"You are analyzing an AI coding session named '{session_name}' (role: {role}).",
        f"The session {'has assigned work' if has_pending_work else 'has no explicitly assigned work'}.",
        "",
        "Here is the current terminal snapshot (what the session shows right now):",
        f"```\n{snapshot[-1500:]}\n```",
    ]

    if transcript_delta:
        parts.extend([
            "",
            "Here is the recent transcript activity (new since last check):",
            f"```\n{transcript_delta[-2000:]}\n```",
        ])

    parts.extend([
        "",
        "Return a single JSON object with these fields:",
        "",
        '  "action": "proceed" | "nudge" | "idle"',
        '    - proceed: the session outlined next steps or asked "should I continue?" — tell it to go ahead',
        '    - nudge: the session is stuck on an error or has an obvious next step it hasn\'t taken',
        '    - idle: the session genuinely finished everything',
        '    Default to "proceed" if the session proposed a plan. Workers should keep working, not wait for permission.',
        "",
        '  "action_message": "what to send to the session" (only if action is proceed or nudge)',
        "",
        '  "knowledge_entries": array of {"kind": "decision|goal|risk|idea|architecture|convention", "text": "..."}',
        "    Extract concrete project facts from the transcript. Be specific. Skip routine tool calls.",
        "",
        '  "activity_summary": "1-3 sentences describing what happened this cycle"',
        "    Be specific: what was discussed, what was built, what was decided.",
        "",
        '  "has_work_product": true/false',
        "    True if there are commits, file changes, deploys, or other tangible output.",
        "",
        "Return ONLY valid JSON, no markdown fences.",
    ])

    return "\n".join(parts)


def _parse_result(raw: dict[str, Any]) -> SessionIntelligenceResult:
    entries = []
    for entry in raw.get("knowledge_entries", []):
        if isinstance(entry, dict) and "kind" in entry and "text" in entry:
            text = str(entry["text"]).strip()
            if text and len(text) < 500:
                entries.append({"kind": str(entry["kind"]), "text": text})

    return SessionIntelligenceResult(
        action=str(raw.get("action", "idle")),
        action_message=str(raw.get("action_message", "")),
        knowledge_entries=entries,
        activity_summary=str(raw.get("activity_summary", "")),
        has_work_product=bool(raw.get("has_work_product", False)),
    )


# ---------------------------------------------------------------------------
# Pending knowledge staging
# ---------------------------------------------------------------------------

def _pending_knowledge_dir(project_root: Path) -> Path:
    return project_root / ".pollypm" / "pending-knowledge"


def stage_pending_knowledge(
    project_root: Path,
    session_name: str,
    entries: list[dict],
) -> None:
    """Write pending knowledge entries to staging area for Opus to process."""
    if not entries:
        return
    dest = _pending_knowledge_dir(project_root) / session_name
    dest.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = dest / f"{ts}.json"
    atomic_write_text(path, json.dumps(entries, indent=2) + "\n")


def read_pending_knowledge(project_root: Path) -> list[dict]:
    """Read all pending knowledge entries across all sessions."""
    root = _pending_knowledge_dir(project_root)
    if not root.exists():
        return []
    entries: list[dict] = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        for path in sorted(session_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                if isinstance(data, list):
                    entries.extend(data)
            except (json.JSONDecodeError, OSError):
                continue
    return entries


def clear_pending_knowledge(project_root: Path) -> int:
    """Clear all pending knowledge files after Opus processes them."""
    root = _pending_knowledge_dir(project_root)
    if not root.exists():
        return 0
    count = 0
    for session_dir in list(root.iterdir()):
        if not session_dir.is_dir():
            continue
        for path in list(session_dir.glob("*.json")):
            path.unlink(missing_ok=True)
            count += 1
        # Remove empty session dirs
        try:
            session_dir.rmdir()
        except OSError:
            pass
    return count


# ---------------------------------------------------------------------------
# 5-minute sweep — processes ALL sessions with new transcript activity
# ---------------------------------------------------------------------------

_CURSOR_FILE = ".session-intelligence-state.json"


def _cursor_path(project_root: Path) -> Path:
    return project_root / ".pollypm" / "transcripts" / _CURSOR_FILE


def _load_cursors(project_root: Path) -> dict[str, int]:
    path = _cursor_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {str(k): int(v) for k, v in data.get("files", {}).items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cursors(project_root: Path, cursors: dict[str, int]) -> None:
    path = _cursor_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"files": cursors}, indent=2) + "\n")


def _read_new_events(project_root: Path, session_name: str, cursors: dict[str, int]) -> tuple[list[dict], int]:
    """Read new transcript events for one session since the cursor position."""
    events_path = project_root / ".pollypm" / "transcripts" / session_name / "events.jsonl"
    if not events_path.exists():
        return [], 0
    key = f"{session_name}/events.jsonl"
    offset = cursors.get(key, 0)
    try:
        size = events_path.stat().st_size
    except OSError:
        return [], offset
    if size <= offset:
        return [], offset  # No new data
    if size < offset:
        offset = 0  # File was truncated
    events: list[dict] = []
    with events_path.open("r", encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
                if isinstance(event, dict):
                    events.append(event)
            except (json.JSONDecodeError, ValueError):
                continue
        new_offset = f.tell()
    return events, new_offset


def _events_to_transcript_delta(events: list[dict], max_chars: int = 2000) -> str:
    """Convert events to a readable transcript delta for the Haiku prompt."""
    lines: list[str] = []
    total = 0
    for event in events:
        etype = event.get("event_type", "")
        payload = event.get("payload") or {}
        text = str(payload.get("text", ""))[:300]
        if etype in ("user_turn", "assistant_turn") and text:
            role = "USER" if etype == "user_turn" else "ASSISTANT"
            line = f"{role}: {text}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        elif etype == "commit":
            msg = str(payload.get("message", ""))[:100]
            lines.append(f"COMMIT: {msg}")
    return "\n".join(lines)


def sweep_all_sessions(config) -> dict[str, int]:
    """Process ALL sessions with new transcript events since last cursor.

    This is the ONLY Haiku path. One call per session with new data.
    Returns triage actions + knowledge entries + activity summaries.
    Triage actions are applied immediately (send_input to workers).

    Returns: {sessions_processed, knowledge_entries, summaries, actions_taken}
    """
    from pollypm.knowledge_extract import _all_project_roots
    from pollypm.supervisor import Supervisor

    counts = {"sessions_processed": 0, "knowledge_entries": 0, "summaries": 0, "actions_taken": 0}
    sup = None

    for project_root in _all_project_roots(config):
        cursors = _load_cursors(project_root)
        transcript_root = project_root / ".pollypm" / "transcripts"
        if not transcript_root.exists():
            continue

        updated_cursors = dict(cursors)

        for session_dir in sorted(transcript_root.iterdir()):
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            session_name = session_dir.name

            events, new_offset = _read_new_events(project_root, session_name, cursors)
            if not events:
                continue

            transcript_delta = _events_to_transcript_delta(events)
            if not transcript_delta:
                updated_cursors[f"{session_name}/events.jsonl"] = new_offset
                continue

            # Get current snapshot for triage
            snapshot = ""
            try:
                if sup is None:
                    sup = Supervisor(config)
                snapshot_path = config.project.snapshots_dir / f"{session_name}.txt"
                if snapshot_path.exists():
                    snapshot = snapshot_path.read_text()[-1500:]
            except Exception:  # noqa: BLE001
                pass

            session_cfg = config.sessions.get(session_name)
            role = session_cfg.role if session_cfg else "worker"

            intel = run_session_intelligence(
                snapshot=snapshot,
                transcript_delta=transcript_delta,
                session_name=session_name,
                role=role,
                has_pending_work=True,
            )

            if intel is not None:
                # Act on triage — push workers forward
                if intel.action in ("proceed", "nudge") and role == "worker":
                    try:
                        if sup is None:
                            sup = Supervisor(config)
                        msg = intel.action_message or "Continue working."
                        sup.send_input(session_name, msg, owner="pollypm", force=True)
                        counts["actions_taken"] += 1
                    except Exception:  # noqa: BLE001
                        pass

                if intel.knowledge_entries:
                    stage_pending_knowledge(project_root, session_name, intel.knowledge_entries)
                    counts["knowledge_entries"] += len(intel.knowledge_entries)
                if intel.activity_summary:
                    _append_activity_summary(project_root, intel.activity_summary)
                    counts["summaries"] += 1

            updated_cursors[f"{session_name}/events.jsonl"] = new_offset
            counts["sessions_processed"] += 1

        _save_cursors(project_root, updated_cursors)

    if sup is not None:
        sup.store.close()

    return counts


def _append_activity_summary(project_root: Path, summary: str) -> None:
    """Append a one-line activity summary to the activity log."""
    log_path = project_root / "docs" / "activity-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    entry = f"- **{ts}** — {summary}\n"
    if log_path.exists():
        existing = log_path.read_text()
    else:
        existing = "# Activity Log\n\n"
    if "\n\n" in existing:
        header, body = existing.split("\n\n", 1)
        log_path.write_text(f"{header}\n\n{entry}{body}")
    else:
        log_path.write_text(f"{existing}\n{entry}")
