"""Recovery prompt construction and injection.

Builds structured recovery prompts from checkpoint data to re-orient
a fresh agent session to continue where the previous one left off.
Prompts are provider-specific and size-capped with priority-based
truncation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pollypm.checkpoints import CheckpointData, load_canonical_checkpoint
from pollypm.models import PollyPMConfig, ProviderKind


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default max characters for recovery prompt (~8000 tokens)
DEFAULT_MAX_CHARS = 32000

# Section priority for truncation (higher = trimmed first)
SECTION_TRUNCATION_PRIORITY = {
    "project_context": 5,     # Trimmed first — can be summarized
    "blockers": 1,            # Keep — critical for avoiding issues
    "what_to_do_next": 1,     # Keep — most actionable
    "what_completed": 2,      # Important but can be trimmed
    "what_working_on": 1,     # Keep — core context
    "git_state": 3,           # Can be rediscovered
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecoveryPromptSection:
    """A single section of the recovery prompt."""

    key: str
    heading: str
    content: str
    priority: int = 3  # lower = more important


@dataclass(slots=True)
class RecoveryPrompt:
    """Complete recovery prompt with sections."""

    sections: list[RecoveryPromptSection] = field(default_factory=list)
    provider: ProviderKind = ProviderKind.CLAUDE
    checkpoint_id: str = ""
    session_name: str = ""
    is_fallback: bool = False
    recovery_mode_banner: str = ""

    def render(self) -> str:
        """Render the full recovery prompt as text."""
        if self.provider == ProviderKind.CODEX:
            return _render_codex(self.sections, self.recovery_mode_banner, self.is_fallback)
        return _render_claude(self.sections, self.recovery_mode_banner, self.is_fallback)

    @property
    def total_chars(self) -> int:
        return sum(len(s.content) for s in self.sections)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_recovery_prompt(
    config: PollyPMConfig,
    session_name: str,
    project_key: str,
    *,
    provider: ProviderKind = ProviderKind.CLAUDE,
    task_prompt: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> RecoveryPrompt:
    """Build a recovery prompt from checkpoint data.

    Falls back to project context + task prompt if no checkpoint exists.
    """
    checkpoint = load_canonical_checkpoint(config, session_name, project_key)

    if checkpoint is None:
        return _build_fallback_prompt(
            config, project_key,
            provider=provider,
            task_prompt=task_prompt,
            max_chars=max_chars,
        )

    return _build_from_checkpoint(
        config, checkpoint,
        provider=provider,
        task_prompt=task_prompt,
        max_chars=max_chars,
    )


def _build_from_checkpoint(
    config: PollyPMConfig,
    checkpoint: CheckpointData,
    *,
    provider: ProviderKind,
    task_prompt: str,
    max_chars: int,
) -> RecoveryPrompt:
    """Build recovery prompt from checkpoint data."""
    sections: list[RecoveryPromptSection] = []
    recovery_mode_banner = _checkpoint_banner(
        checkpoint_id=checkpoint.checkpoint_id,
        is_fallback=False,
        state=_checkpoint_state(checkpoint),
    )

    # Section 1: Project Context
    project_context = _load_project_context(config, checkpoint.project)
    if project_context:
        sections.append(RecoveryPromptSection(
            key="project_context",
            heading="Project Context",
            content=project_context,
            priority=SECTION_TRUNCATION_PRIORITY["project_context"],
        ))

    # Section 2: What You Were Working On
    working_on_parts: list[str] = []
    if checkpoint.objective:
        working_on_parts.append(f"You were working on: {checkpoint.objective}")
    if checkpoint.sub_step:
        working_on_parts.append(f"You were at step: {checkpoint.sub_step}")
    if task_prompt:
        working_on_parts.append(f"Original task: {task_prompt}")
    if working_on_parts:
        sections.append(RecoveryPromptSection(
            key="what_working_on",
            heading="What You Were Working On",
            content="\n".join(working_on_parts),
            priority=SECTION_TRUNCATION_PRIORITY["what_working_on"],
        ))

    # Section 3: What Was Completed
    if checkpoint.work_completed:
        completed_lines = ["Completed so far:"]
        for item in checkpoint.work_completed:
            completed_lines.append(f"- {item}")
        sections.append(RecoveryPromptSection(
            key="what_completed",
            heading="What Was Completed",
            content="\n".join(completed_lines),
            priority=SECTION_TRUNCATION_PRIORITY["what_completed"],
        ))

    # Section 4: Current Git State (live, not from checkpoint)
    git_state = _live_git_state(config, checkpoint.project)
    if git_state:
        sections.append(RecoveryPromptSection(
            key="git_state",
            heading="Current File State",
            content=git_state,
            priority=SECTION_TRUNCATION_PRIORITY["git_state"],
        ))

    # Section 5: What To Do Next
    if checkpoint.recommended_next_step:
        sections.append(RecoveryPromptSection(
            key="what_to_do_next",
            heading="What To Do Next",
            content=f"Next step: {checkpoint.recommended_next_step}",
            priority=SECTION_TRUNCATION_PRIORITY["what_to_do_next"],
        ))

    # Section 6: Blockers and Open Questions
    blocker_parts: list[str] = []
    if checkpoint.blockers:
        blocker_parts.append("Blockers:")
        for item in checkpoint.blockers:
            blocker_parts.append(f"- {item}")
    if checkpoint.unresolved_questions:
        blocker_parts.append("Open questions:")
        for item in checkpoint.unresolved_questions:
            blocker_parts.append(f"- {item}")
    if blocker_parts:
        sections.append(RecoveryPromptSection(
            key="blockers",
            heading="Blockers and Open Questions",
            content="\n".join(blocker_parts),
            priority=SECTION_TRUNCATION_PRIORITY["blockers"],
        ))

    # Section: Pending inbox items
    inbox_section = _pending_inbox_section(config)
    if inbox_section:
        sections.append(inbox_section)

    # Truncate to fit max_chars
    sections = _truncate_sections(sections, max_chars)

    return RecoveryPrompt(
        sections=sections,
        provider=provider,
        checkpoint_id=checkpoint.checkpoint_id,
        session_name=checkpoint.session_name,
        recovery_mode_banner=recovery_mode_banner,
    )


def _pending_inbox_section(config: PollyPMConfig) -> RecoveryPromptSection | None:
    """Build a section listing tasks waiting on the user / agent.

    Queries the work service for inbox tasks across all tracked projects
    and summarises them. No legacy inbox.
    """
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService

        summaries: list[str] = []
        total = 0
        for project_key, project in getattr(config, "projects", {}).items():
            db_path = project.path / ".pollypm" / "state.db"
            if not db_path.exists():
                continue
            try:
                with SQLiteWorkService(
                    db_path=db_path, project_path=project.path,
                ) as svc:
                    tasks = inbox_tasks(svc, project=project_key)
            except Exception:  # noqa: BLE001
                continue
            for t in tasks[:5]:
                summaries.append(f"  - [{project_key}] {t.title} ({t.work_status.value})")
            total += len(tasks)
        if total == 0:
            return None
        parts = [f"You have {total} task(s) waiting on the user:", *summaries[:10]]
        parts.append("Check with: pm inbox")
        return RecoveryPromptSection(
            key="pending_inbox",
            heading="Pending Inbox Items",
            content="\n".join(parts),
            priority=2,  # Important but not as critical as blockers
        )
    except Exception:  # noqa: BLE001
        return None


def _build_fallback_prompt(
    config: PollyPMConfig,
    project_key: str,
    *,
    provider: ProviderKind,
    task_prompt: str,
    max_chars: int,
) -> RecoveryPrompt:
    """Build fallback prompt when no checkpoint exists."""
    sections: list[RecoveryPromptSection] = []
    recovery_mode_banner = _checkpoint_banner(
        checkpoint_id="",
        is_fallback=True,
        state="",
    )

    project_context = _load_project_context(config, project_key)
    if project_context:
        sections.append(RecoveryPromptSection(
            key="project_context",
            heading="Project Context",
            content=project_context,
            priority=SECTION_TRUNCATION_PRIORITY["project_context"],
        ))

    if task_prompt:
        sections.append(RecoveryPromptSection(
            key="what_working_on",
            heading="Your Task",
            content=task_prompt,
            priority=SECTION_TRUNCATION_PRIORITY["what_working_on"],
        ))

    git_state = _live_git_state(config, project_key)
    if git_state:
        sections.append(RecoveryPromptSection(
            key="git_state",
            heading="Current File State",
            content=git_state,
            priority=SECTION_TRUNCATION_PRIORITY["git_state"],
        ))

    inbox_section = _pending_inbox_section(config)
    if inbox_section:
        sections.append(inbox_section)

    sections = _truncate_sections(sections, max_chars)

    return RecoveryPrompt(
        sections=sections,
        provider=provider,
        is_fallback=True,
        recovery_mode_banner=recovery_mode_banner,
    )


# ---------------------------------------------------------------------------
# Provider-specific rendering
# ---------------------------------------------------------------------------


def _render_claude(
    sections: list[RecoveryPromptSection],
    recovery_mode_banner: str = "",
    is_fallback: bool = False,
) -> str:
    """Render recovery prompt for Claude CLI."""
    parts: list[str] = [
    ]
    if recovery_mode_banner:
        parts.append(recovery_mode_banner)
    if is_fallback:
        parts.extend([
            "This is a fresh launch, not a resuming checkpoint.",
            "Use the context below to start cleanly instead of looking for prior state.",
        ])
    elif recovery_mode_banner:
        parts.extend([
            "Your previous session was interrupted and has been restarted. You are resuming work.",
            "The context below describes what you were doing. Resume your work — do NOT",
            "treat this as a new task or analysis request. Pick up where you left off.",
        ])
    else:
        parts.extend([
            "RECOVERY: Your previous session was interrupted and has been restarted. You are resuming work.",
            "The context below describes what you were doing. Resume your work — do NOT",
            "treat this as a new task or analysis request. Pick up where you left off.",
        ])
    parts.append("")
    for section in sections:
        parts.append(f"## {section.heading}")
        parts.append("")
        parts.append(section.content)
        parts.append("")
    return "\n".join(parts).rstrip()


def _render_codex(
    sections: list[RecoveryPromptSection],
    recovery_mode_banner: str = "",
    is_fallback: bool = False,
) -> str:
    """Render recovery prompt for Codex CLI."""
    parts: list[str] = []
    if recovery_mode_banner:
        parts.append(recovery_mode_banner)
    if is_fallback:
        parts.extend([
            "Fresh launch: there is no checkpoint to resume from.",
            "Use the context below to start cleanly.",
        ])
    elif recovery_mode_banner:
        parts.append("You are resuming an interrupted session.")
    else:
        parts.extend([
            "RECOVERY CONTEXT: You are resuming an interrupted session.",
            "",
        ])
    parts.append("")
    for section in sections:
        parts.append(f"### {section.heading}")
        parts.append("")
        parts.append(section.content)
        parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Section truncation
# ---------------------------------------------------------------------------


def _truncate_sections(
    sections: list[RecoveryPromptSection],
    max_chars: int,
) -> list[RecoveryPromptSection]:
    """Truncate sections to fit within max_chars, trimming by priority."""
    total = sum(len(s.content) + len(s.heading) + 10 for s in sections)  # +10 for formatting
    if total <= max_chars:
        return sections

    # Sort by priority descending (highest priority trimmed first)
    by_priority = sorted(enumerate(sections), key=lambda x: -x[1].priority)

    result = list(sections)
    for idx, section in by_priority:
        total = sum(len(s.content) + len(s.heading) + 10 for s in result)
        if total <= max_chars:
            break

        overshoot = total - max_chars
        content = result[idx].content
        if len(content) > overshoot + 100:
            # Trim content to fit
            trimmed = content[: len(content) - overshoot - 50] + "\n\n[... truncated for context window]"
            result[idx] = RecoveryPromptSection(
                key=section.key,
                heading=section.heading,
                content=trimmed,
                priority=section.priority,
            )
        elif len(content) > 200:
            # Aggressive trim
            result[idx] = RecoveryPromptSection(
                key=section.key,
                heading=section.heading,
                content=content[:200] + "\n\n[... truncated]",
                priority=section.priority,
            )

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_project_context(config: PollyPMConfig, project_key: str) -> str:
    """Load project context from docs/project-overview.md.

    Only extracts the Summary section to avoid injecting raw analysis
    output or corrupted content from recent imports.
    """
    project_root = _project_root(config, project_key)
    overview_path = project_root / "docs" / "project-overview.md"
    if not overview_path.exists():
        return ""
    try:
        content = overview_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Reject content that looks like raw JSON/analysis output
    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return ""
    if "CATASTROPHIC" in content or "UNRECOVERABLE" in content:
        return ""  # Corrupted by stale analysis data

    title_line = ""
    for line in content.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("# "):
            title_line = stripped_line
            break

    # Extract just the Summary section if present
    if "## Summary" in content:
        start = content.index("## Summary")
        # Find the next ## heading or end of file
        rest = content[start + len("## Summary"):]
        end = rest.find("\n## ")
        summary = rest[:end].strip() if end != -1 else rest.strip()
        if summary and len(summary) > 20:
            title_prefix = f"{title_line}\n\n" if title_line else ""
            return f"{title_prefix}## Project Summary\n{summary}"

    # Fallback: first 2000 chars, but skip if it's too long (likely corrupted)
    if len(content) > 10000:
        return content[:2000] + "\n...(truncated)"
    return content[:4000]


def _checkpoint_state(checkpoint: CheckpointData) -> str:
    """Return the most useful one-line summary of the checkpoint state."""
    return checkpoint.sub_step or checkpoint.objective or checkpoint.role or "unknown"


def _checkpoint_banner(*, checkpoint_id: str, is_fallback: bool, state: str) -> str:
    """Return the explicit banner for the recovery mode."""
    if is_fallback:
        return "RECOVERY MODE: FRESH LAUNCH — no checkpoint to resume from."
    return f"RECOVERY MODE: RESUMING FROM CHECKPOINT {checkpoint_id} — last state was {state}."


def _live_git_state(config: PollyPMConfig, project_key: str) -> str:
    """Get live git state for the project."""
    project_root = _project_root(config, project_key)
    if not (project_root / ".git").is_dir():
        return ""

    parts: list[str] = ["Git state:"]

    branch = _git_output(str(project_root), ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        parts.append(f"- Branch: {branch}")

    status = _git_output(str(project_root), ["git", "status", "--porcelain"])
    if status:
        changed = len(status.strip().splitlines())
        parts.append(f"- Uncommitted changes: {changed} file(s)")
    else:
        parts.append("- Working tree clean")

    return "\n".join(parts) if len(parts) > 1 else ""


def _git_output(cwd: str, command: list[str]) -> str:
    """Run a git command and return stdout."""
    try:
        result = subprocess.run(
            command, cwd=cwd,
            check=False, capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _project_root(config: PollyPMConfig, project_key: str) -> Path:
    if project_key == "pollypm":
        return config.project.root_dir
    project = config.projects.get(project_key)
    if project is None:
        return config.project.root_dir
    return project.path
