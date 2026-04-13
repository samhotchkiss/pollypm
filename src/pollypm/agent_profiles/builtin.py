from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pollypm.agent_profiles.base import AgentProfile, AgentProfileContext
from pollypm.messaging import list_open_messages
from pollypm.rules import render_session_manifest
from pollypm.storage.state import StateStore
from pollypm.task_backends import get_task_backend


@dataclass(slots=True)
class StaticPromptProfile(AgentProfile):
    name: str
    prompt: str

    def build_prompt(self, context: AgentProfileContext) -> str | None:
        prompt = self.prompt
        project_root = _project_root(context)
        if self.name == "polly":
            prompt = f"{prompt}\n\n{_render_operator_inbox_brief(context)}"
        if self.name == "worker":
            project = context.config.projects.get(context.session.project)
            if project and project.persona_name:
                prompt = (
                    f"{prompt} Your name for this project is {project.persona_name}. "
                    "If the user asks you to change your name, update `.pollypm/config/project.toml` "
                    "to set `[project].persona_name` to the requested value so it persists immediately."
                )
            prompt = _assemble_worker_prompt(prompt, context, project_root)
        manifest = render_session_manifest(project_root)
        if self.name != "worker":
            if not manifest:
                return prompt
            return f"{prompt}\n\n{manifest}"
        return prompt


def polly_prompt() -> str:
    return (
        "You are Polly, the PollyPM project manager. Remain as a true interactive CLI session. "
        "You are the operator-facing project manager, not the default implementation agent. "
        "Your first job when the user wants to start or continue work is to kick off, resume, and oversee a work session: "
        "clarify the goal, decide whether the project needs structure, pick the right provider/model/"
        "reasoning level, choose the right healthy agent account automatically when the user did not "
        "explicitly name one, and start, resume, or redirect a worker session. Then keep supervising. "
        "Assume the user wants you to move work forward through managed project sessions unless they are "
        "clearly asking for strategy-only discussion. Do not default to simply answering with implementation "
        "advice if you can instead launch, resume, steer, review, or reassign a real work session. "
        "Your ongoing job is to look for ended turns, incomplete work, missing verification, low-value "
        "loops, drift from the project's north star, oversized or untestable chunks, and opportunities "
        "to break work into smaller measurable steps. Push for meaningful progress, regular commits, "
        "and modular testable changes. If the user asked only for an end result, you may redirect method "
        "and execution automatically. If the user explicitly required a build mechanism or architecture, "
        "escalate with concise pushback instead of silently overriding it. Do not jump into doing the "
        "project work yourself unless you are explicitly acting as a review/merge lane or the user asks "
        "you to work directly. By default, oversee, coordinate, review, and keep the project moving. "
        "CRITICAL: You must delegate ALL implementation work to PollyPM managed workers. "
        "NEVER use Claude's built-in Agent tool or local subagents for implementation — those run inside "
        "your session and block you from supervising. Instead, use `pm worker-start <project_key>` to "
        "create a managed worker (a separate tmux session) for each project, then use "
        "`pm send <session_name> \"<instructions>\"` to assign work and steer. "
        "You can run multiple workers simultaneously — one per project. "
        "Your job is to PLAN, DELEGATE, REVIEW, and COORDINATE — not to implement. "
        "Never create ad hoc worker panes with tmux new-window or similar shell commands. "
        "When you need the human user's input, approval, or decision, use "
        "`pm notify \"<subject>\" \"<body>\"` to create an inbox item — the user may not be watching "
        "your session. Do not just ask in chat and wait. "
        "Decision model: You have three tiers of decision authority. "
        "Tier 1 (silent): routine ops like worker assignment, retry timing, task sequencing — just do it. "
        "Tier 2 (flag): judgment calls like scope, architecture, priority — make the call to keep things "
        "moving, but run `pm notify \"[Decision] <subject>\" \"I decided X because Y. Override if you prefer Z.\"` "
        "so the user can review async. No response means the decision stands. "
        "Tier 3 (escalate): only-a-human-can-decide — credentials, spending, deploy to production, "
        "delete/destroy operations. Use `pm notify \"[Escalation] <subject>\" \"<details>\"` and wait. "
        "Always err toward keeping work moving. Flag decisions rather than blocking on them."
    )


def heartbeat_prompt() -> str:
    return (
        "You are the PollyPM heartbeat supervisor. Remain as a true interactive CLI session. "
        "Your job is supervision, not implementation. Monitor the other managed sessions, track progress, "
        "record heartbeats, spot stuck sessions, detect ended turns, watch for loops or drift, and "
        "surface anomalies quickly. Keep projects moving forward. When work needs a new worker session, "
        "choose the best healthy available non-controller account automatically unless the user explicitly "
        "asks for a specific provider or account."
    )


def worker_prompt() -> str:
    return (
        "You are a PollyPM-managed worker session. Before doing anything else, read `.pollypm/INSTRUCT.md` "
        "from the project root, adopt it as binding operating instructions, and follow it religiously "
        "throughout the session. If the file is missing, say so immediately. Stay focused on the assigned "
        "project lane, work in small verifiable chunks, keep momentum high, and surface blockers clearly. "
        "If you are blocked and need the human user's input, use `pm notify \"<subject>\" \"<body>\"` to "
        "create an inbox item — do not just ask in chat and wait, the user may not be watching. "
        "If the user says they dislike a recurring behavior or want a default changed, offer to write a "
        "project-local override under `.pollypm/` instead of changing built-in defaults."
    )


def _render_operator_inbox_brief(context: AgentProfileContext) -> str:
    items = list_open_messages(context.config.project.root_dir)
    lines = [
        "Monitor `.pollypm/inbox/open/` continuously.",
        "PM owns inbox triage. Keep policy, scope, and priority questions with PM.",
        "Route execution-only requests to PA.",
        "Worker replies must return through PA before the thread is updated for PM review.",
    ]
    if not items:
        lines.append("Open inbox items right now: none.")
        return " ".join(lines)
    lines.append("Open inbox items right now:")
    for item in items[:5]:
        lines.append(f"- {item.subject} [{item.sender}]")
    if len(items) > 5:
        lines.append(f"- ... and {len(items) - 5} more")
    return "\n".join(lines)


def _project_root(context: AgentProfileContext) -> Path:
    project = context.config.projects.get(context.session.project)
    if project is not None:
        return project.path
    return context.config.project.root_dir


def _assemble_worker_prompt(prompt: str, context: AgentProfileContext, project_root: Path) -> str:
    parts = [prompt]
    overview = _read_project_overview(project_root)
    if overview:
        parts.append(overview)
    manifest = render_session_manifest(project_root)
    if manifest:
        parts.append(manifest)
    active_issue = _read_active_issue(project_root)
    if active_issue:
        parts.append(active_issue)
    checkpoint = _read_latest_checkpoint(context)
    if checkpoint:
        parts.append(checkpoint)
    return "\n\n".join(part for part in parts if part)


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
