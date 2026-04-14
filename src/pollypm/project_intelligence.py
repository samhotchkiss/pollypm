"""Project Intelligence — Opus doc maintenance.

Runs hourly. For each project with pending knowledge entries from the
Haiku session intelligence tier, makes one Opus call to rewrite project
docs. Only updates docs where material changes occurred. Commits to git.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.llm_runner import run_opus_json
from pollypm.session_intelligence import clear_pending_knowledge, read_pending_knowledge

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)

# Docs that Opus can rewrite
_DOC_NAMES = ["project-overview", "decisions", "risks", "ideas"]


def run_project_intelligence(config: PollyPMConfig, project_root: Path) -> bool:
    """Single Opus call per project. Rewrites docs only if material changes.

    Returns True if docs were updated and committed.
    """
    pending = read_pending_knowledge(project_root)
    if not pending:
        return False

    # Read current docs
    current_docs = _read_current_docs(project_root)

    # Read recent activity log
    activity = _read_recent_activity(project_root)

    # Read git log
    git_log = _read_git_log(project_root)

    # Build prompt
    prompt = _build_opus_prompt(current_docs, pending, activity, git_log)

    # Call Opus
    result = run_opus_json(prompt)
    if result is None:
        logger.warning("Opus call failed for %s — pending knowledge preserved for next cycle", project_root.name)
        return False

    material_changes = result.get("material_changes", False)
    if not material_changes:
        logger.info("Opus: no material changes for %s", project_root.name)
        clear_pending_knowledge(project_root)
        return False

    # Apply updates
    updates = result.get("updates", {})
    written = _write_doc_updates(project_root, updates)
    if not written:
        logger.info("Opus returned material_changes=true but no doc content for %s", project_root.name)
        clear_pending_knowledge(project_root)
        return False

    # Commit
    committed = _git_commit_docs(project_root)

    # Clear pending knowledge
    cleared = clear_pending_knowledge(project_root)
    logger.info(
        "Project intelligence for %s: updated %d docs, committed=%s, cleared %d pending entries",
        project_root.name, written, committed, cleared,
    )
    return True


def _read_current_docs(project_root: Path) -> dict[str, str]:
    docs = {}
    for name in _DOC_NAMES:
        path = project_root / "docs" / f"{name}.md"
        if path.exists():
            docs[name] = path.read_text().strip()
        else:
            docs[name] = ""
    return docs


def _read_recent_activity(project_root: Path) -> str:
    path = project_root / "docs" / "activity-log.md"
    if not path.exists():
        return ""
    content = path.read_text()
    # Take last ~2000 chars (most recent entries are at the top)
    return content[:2000]


def _read_git_log(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since=2 hours ago", "-20"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _build_opus_prompt(
    current_docs: dict[str, str],
    pending_entries: list[dict],
    activity: str,
    git_log: str,
) -> str:
    parts = [
        "You are maintaining project documentation. Your job is to keep these docs "
        "accurate and current based on new information from the development team.",
        "",
        "RULES:",
        "- ONLY update a doc if there is a MATERIAL change — a new decision, changed scope, "
        "new risk, reversed direction, or significant new information.",
        "- If a new decision contradicts an old one, REPLACE the old one. Don't keep both.",
        "- If scope expanded or contracted, UPDATE the scope section.",
        "- Do NOT rewrite docs just to improve wording or reorganize. Only factual changes matter.",
        "- Return null for any doc that doesn't need changes.",
        "- Keep docs concise. Use bullet points. No fluff.",
        "",
    ]

    for name, content in current_docs.items():
        parts.append(f"## Current {name}.md")
        if content:
            parts.append(f"```\n{content[:3000]}\n```")
        else:
            parts.append("(empty — this is a new doc)")
        parts.append("")

    parts.append("## New information from development sessions")
    for entry in pending_entries[:100]:  # Cap at 100 entries
        kind = entry.get("kind", "info")
        text = entry.get("text", "")
        parts.append(f"- [{kind}] {text}")
    parts.append("")

    if activity:
        parts.append("## Recent activity log")
        parts.append(f"```\n{activity}\n```")
        parts.append("")

    if git_log:
        parts.append("## Recent commits")
        parts.append(f"```\n{git_log}\n```")
        parts.append("")

    parts.extend([
        "Return JSON:",
        '{',
        '  "material_changes": true/false,',
        '  "updates": {',
        '    "project-overview": "full doc content" or null,',
        '    "decisions": "full doc content" or null,',
        '    "risks": "full doc content" or null,',
        '    "ideas": "full doc content" or null',
        '  },',
        '  "reasoning": "one sentence explaining what changed and why"',
        '}',
        "",
        "Return ONLY valid JSON, no markdown fences.",
    ])

    return "\n".join(parts)


def _write_doc_updates(project_root: Path, updates: dict) -> int:
    if not isinstance(updates, dict):
        return 0
    written = 0
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name in _DOC_NAMES:
        content = updates.get(name)
        if content is None or not isinstance(content, str) or not content.strip():
            continue
        path = docs_dir / f"{name}.md"
        path.write_text(content.strip() + "\n")
        written += 1
    return written


def _git_commit_docs(project_root: Path) -> bool:
    try:
        subprocess.run(
            ["git", "add", "docs/"],
            cwd=project_root, capture_output=True, timeout=10,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_root, capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return False  # Nothing staged
        subprocess.run(
            ["git", "commit", "-m", "docs: update project docs (opus intelligence)"],
            cwd=project_root, capture_output=True, timeout=15,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
