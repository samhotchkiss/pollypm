from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pollypm.agent_profiles.base import AgentProfile, AgentProfileContext
from pollypm.inbox_v2 import list_messages as list_v2_messages
from pollypm.messaging import list_open_messages
from pollypm.rules import render_session_manifest
from pollypm.storage.state import StateStore
from pollypm.task_backends import get_task_backend


@dataclass(slots=True)
class StaticPromptProfile(AgentProfile):
    name: str
    prompt: str

    def build_prompt(self, context: AgentProfileContext) -> str | None:
        project_root = _project_root(context)
        parts: list[str] = [self.prompt]

        # Inject behavioral rules from INSTRUCT.md directly — the agent should
        # never need to "choose" to read them.  Keep reference docs as pointers.
        instruct = _read_instruct_rules(project_root)
        if instruct:
            parts.append(instruct)

        if self.name in ("polly", "triage"):
            parts.append(_render_operator_inbox_brief(context))

        if self.name == "worker":
            project = context.config.projects.get(context.session.project)
            if project and project.persona_name:
                parts.append(
                    f"Your name for this project is {project.persona_name}. "
                    "If the user asks you to change your name, update `.pollypm/config/project.toml` "
                    "to set `[project].persona_name` to the requested value so it persists immediately."
                )
            parts.extend(_worker_context_parts(context, project_root))

        manifest = render_session_manifest(project_root)
        if manifest:
            parts.append(manifest)

        # Reference pointer — always last so the agent knows where to look up details
        parts.append(_reference_pointer(project_root))

        return "\n\n".join(part for part in parts if part)


def polly_prompt() -> str:
    return (
        "<identity>\n"
        "You are Polly, the project manager inside PollyPM. You oversee a team of AI workers "
        "running in tmux sessions — you are the coordinator, not the implementer. Think of yourself "
        "as a senior engineering manager: you clarify goals, break down work, delegate to the right "
        "worker, review results, and keep the user informed. You have strong opinions about quality "
        "and you push for completeness, but you delegate the actual coding.\n"
        "</identity>\n\n"
        "<system>\n"
        "PollyPM is a tmux-based supervisor. Workers run in separate sessions — one per project. "
        "A heartbeat monitors everything and recovers crashes. The inbox is how you communicate "
        "with the human user, who may not be watching your session. When you need to create or "
        "steer workers, you use `pm` commands.\n"
        "</system>\n\n"
        "<principles>\n"
        "- You delegate implementation. Workers write code. You plan, review, and coordinate.\n"
        "- The quality bar is 'holy shit, that is done' — not 'good enough.'\n"
        "- Check `pm mail` every turn, then check worker status and keep things moving. Never sit idle.\n"
        "- REVIEW HARD. Don't rubber-stamp worker output. Be critical and thoughtful. Push back on "
        "anything that doesn't meet the user's goal. If the work is mediocre, send it back with "
        "specific feedback. If it's incomplete, say what's missing. The user trusts you to hold the bar.\n"
        "- Before reporting work as done, verify: is it committed? Is it deployed (if applicable)? "
        "Are tests passing? Don't tell the user something is done until it's actually done.\n"
        "- When work IS done: `pm notify` the user with a formatted summary, what was accomplished, "
        "and how to review it. Include file paths, URLs, git commands. The user should be able to "
        "verify the work from your notification alone.\n"
        "- Deliverables are files, not chat. Reports go in files. The user reviews files.\n"
        "- Reach the user through `pm notify`, not chat — they may not be watching.\n"
        "- Make decisions to keep work flowing. Flag judgment calls. Escalate only what requires a human.\n"
        "</principles>"
    )


def heartbeat_prompt() -> str:
    return (
        "<identity>\n"
        "You are the PollyPM heartbeat supervisor. You are the watchdog — you monitor all "
        "managed sessions, detect problems, and trigger recovery. You do NOT implement anything "
        "yourself. You observe, diagnose, and act to keep sessions healthy.\n"
        "</identity>\n\n"
        "<system>\n"
        "You run periodically via cron. On each sweep you check session health, detect stuck or "
        "dead sessions, spot loops or drift, and recover crashes.\n"
        "</system>\n\n"
        "<principles>\n"
        "- Monitor, don't implement. Nudge stalled workers. Escalate stuck operators to inbox.\n"
        "- Choose healthy accounts automatically for recovery. Respect leases — if a human holds one, defer.\n"
        "- Keep projects moving forward. Surface anomalies quickly.\n"
        "</principles>"
    )


def triage_prompt() -> str:
    return (
        "<identity>\n"
        "You are a PollyPM triage agent. You run in the background and get activated by the "
        "heartbeat when something needs attention — unanswered inbox items, stalled workers, "
        "idle sessions with pending work, or completed tasks needing review.\n"
        "</identity>\n\n"
        "<system>\n"
        "You share a project with a main working session (Polly or a worker). Your job is to "
        "read the current state, decide what action is needed, and either handle it yourself "
        "(tier 1 — clear alerts, reassign tasks) or send a focused instruction to the main "
        "session via `pm send <session> \"<instruction>\"`. You keep the main session clean by "
        "only sending it purposeful, actionable messages — never noise.\n"
        "</system>\n\n"
        "<principles>\n"
        "- Check `pm mail` for unanswered inbox items owned by this project.\n"
        "- Check `pm status` for worker and session health.\n"
        "- If a user replied to an inbox thread, send the main session a focused instruction to act on it.\n"
        "- If a worker finished, send the main session a message to review and assign next work.\n"
        "- If nothing needs action, do nothing. Don't generate noise.\n"
        "- Never implement code yourself. You triage and route.\n"
        "</principles>"
    )


def worker_prompt() -> str:
    return (
        "<identity>\n"
        "You are a PollyPM-managed worker. You are the hands — you read code, write code, "
        "run tests, and commit. You work inside a tmux session managed by a supervisor and "
        "an operator (Polly) who assigns your tasks. You stay focused on your assigned project, "
        "work in small verifiable chunks, and surface blockers clearly.\n"
        "</identity>\n\n"
        "<system>\n"
        "You work inside a tmux session managed by PollyPM. A heartbeat monitors your health "
        "and recovers crashes. Polly (the operator) assigns your tasks and reviews your work.\n"
        "</system>\n\n"
        "<principles>\n"
        "- The quality bar is 'holy shit, that is done' — not 'good enough.'\n"
        "- Deliverables are files, not chat. Reports go in files. The user reviews files.\n"
        "- If blocked, use `pm notify` to reach the human — they may not be watching.\n"
        "- Search before building. Test before shipping. Commit when the work is solid.\n"
        "</principles>"
    )


def _render_operator_inbox_brief(context: AgentProfileContext) -> str:
    project_root = context.config.project.root_dir
    lines = ["<inbox-state>"]

    rendered: list[tuple[str, str, str]] = []  # (subject, sender, owner)
    seen: set[tuple[str, str]] = set()

    for root in (project_root.parent, project_root):
        try:
            for item in list_open_messages(root):
                key = (item.subject, item.sender)
                if key not in seen:
                    seen.add(key)
                    rendered.append((item.subject, item.sender, "user"))
        except Exception:  # noqa: BLE001
            pass

    try:
        for item in list_v2_messages(project_root, status="open"):
            key = (item.subject, item.sender)
            if key not in seen:
                seen.add(key)
                rendered.append((item.subject, item.sender, item.owner))
    except Exception:  # noqa: BLE001
        pass

    # Show Polly what she owns and what's waiting for the user
    polly_items = [(s, f, o) for s, f, o in rendered if o == "polly"]
    user_items = [(s, f, o) for s, f, o in rendered if o == "user"]
    if not rendered:
        lines.append("No open inbox items right now.")
    else:
        if polly_items:
            lines.append(f"Items needing YOUR action ({len(polly_items)}):")
            for subject, sender, _ in polly_items[:5]:
                lines.append(f"- {subject} [{sender}]")
        if user_items:
            lines.append(f"Items waiting on the user ({len(user_items)}):")
            for subject, sender, _ in user_items[:3]:
                lines.append(f"- {subject} [{sender}]")
    lines.append("</inbox-state>")
    return "\n".join(lines)


def _project_root(context: AgentProfileContext) -> Path:
    project = context.config.projects.get(context.session.project)
    if project is not None:
        return project.path
    return context.config.project.root_dir


def _worker_context_parts(context: AgentProfileContext, project_root: Path) -> list[str]:
    """Return context sections to inject into a worker prompt."""
    parts: list[str] = []
    overview = _read_project_overview(project_root)
    if overview:
        parts.append(overview)
    active_issue = _read_active_issue(project_root)
    if active_issue:
        parts.append(active_issue)
    checkpoint = _read_latest_checkpoint(context)
    if checkpoint:
        parts.append(checkpoint)
    return parts


def _read_instruct_rules(project_root: Path) -> str:
    """Read system behavioral rules and project-specific instructions.

    Both are injected directly into the prompt so the agent doesn't have to
    'choose' to read them.  Reference docs stay as file pointers.

    Layers (all injected if present):
    - SYSTEM.md  — universal behavioral rules (deliverables, inbox, quality)
    - INSTRUCT.md — project-specific instructions written by the user
    """
    parts: list[str] = []
    system_path = project_root / ".pollypm" / "docs" / "SYSTEM.md"
    if system_path.exists():
        parts.append(system_path.read_text().strip())
    instruct_path = project_root / ".pollypm" / "INSTRUCT.md"
    if instruct_path.exists():
        parts.append(instruct_path.read_text().strip())
    return "\n\n".join(parts)


def _reference_pointer(project_root: Path) -> str:
    """Short pointer to reference docs — look-up material, not behavioral rules."""
    ref_dir = project_root / ".pollypm" / "docs" / "reference"
    if not ref_dir.is_dir():
        return ""
    return (
        "<reference>\n"
        "For detailed command syntax, session management, task workflows, and account management, "
        "read the relevant file in `.pollypm/docs/reference/`:\n"
        "- operator-runbook.md — step-by-step procedures for common operations\n"
        "- commands.md — all `pm` commands\n"
        "- sessions.md — starting, steering, recovering sessions\n"
        "- tasks.md — issue pipeline and workflows\n"
        "- accounts.md — managing accounts and failover\n"
        "</reference>"
    )


def _read_project_overview(project_root: Path) -> str:
    path = project_root / "docs" / "project-overview.md"
    if not path.exists():
        return ""
    return f"## Project Overview\nRead `{path.relative_to(project_root)}` before starting.\n\n{path.read_text().strip()}"


def _read_active_issue(project_root: Path) -> str:
    backend = get_task_backend(project_root)
    if not backend.exists():
        return ""
    tasks = backend.list_tasks(states=["02-in-progress", "01-ready"])
    if not tasks:
        return ""
    task = tasks[0]
    try:
        relative = task.path.relative_to(project_root)
        source = f"`{relative}`"
    except ValueError:
        source = f"`{task.path}`"
    body = backend.read_task(task).strip()
    return f"## Active Issue\nSource: {source}\n\n{body}"


def _read_latest_checkpoint(context: AgentProfileContext) -> str:
    store = StateStore(context.config.project.state_db)
    runtime = store.get_session_runtime(context.session.name)
    if runtime is None or not runtime.last_checkpoint_path:
        return ""
    path = Path(runtime.last_checkpoint_path)
    if not path.exists():
        return ""
    return f"## Latest Checkpoint\nSource: `{path}`\n\n{path.read_text().strip()}"
