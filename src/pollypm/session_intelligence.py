"""Session Intelligence — unified Haiku call per session.

Consolidates triage, knowledge extraction, and activity summarization
into a single LLM call. Runs inside the heartbeat sweep for sessions
that are stalled (identical snapshots). Results are acted on immediately
(triage) and staged for later processing (knowledge → Opus).
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
