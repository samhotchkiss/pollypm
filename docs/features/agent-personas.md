**Last Verified:** 2026-04-22

## Summary

Agent personas are the *behavioral contract* injected into each tmux session's CLI when it launches. PollyPM ships five core personas: **Polly** (project manager / operator), **Russell** (reviewer), **worker** (implementer), **heartbeat** (supervisor agent), and **triage** (shared with Polly). Plus plugin-contributed personas: **advisor**, **explorer** (downtime), **herald** (morning briefing), **architect** + five **critics** (project planning), and **magic** (itsalive alias).

Personas are registered as `agent_profiles` on `PollyPMPlugin`. Each profile builds the full prompt in `build_prompt(context)`:

1. Start from the static persona prompt (e.g. `polly_prompt()`).
2. Append `INSTRUCT.md` behavioral rules when present.
3. For Polly / triage, append an operator state brief (project / task / worker overview).
4. For workers, append project persona naming, project context parts.
5. Append the session manifest (rules) from `pollypm.rules.render_session_manifest`.
6. Always end with a reference pointer (where to look up docs).

Memory injection (`build_memory_injection`) and worker-protocol injection (`build_worker_protocol_injection`) are prepended by the session service at launch time — see [modules/memory.md](../modules/memory.md).

Touch this doc when changing core persona text, or when adding a new persona. Keep the prompts **behavioral** — reference material belongs in docs and `pm help`, not in every session's context window.

## Core Contracts

```python
# src/pollypm/agent_profiles/base.py
@dataclass(slots=True)
class AgentProfileContext:
    config: PollyPMConfig
    session: SessionConfig
    account: AccountConfig
    task: Task | None = None
    # ... plus work-service + state-store accessors

class AgentProfile(Protocol):
    name: str
    def build_prompt(self, context: AgentProfileContext) -> str | None: ...

# src/pollypm/plugins_builtin/core_agent_profiles/profiles.py
@dataclass(slots=True)
class StaticPromptProfile(AgentProfile):
    name: str
    prompt: str
    def build_prompt(self, context: AgentProfileContext) -> str | None: ...

def polly_prompt() -> str: ...
def reviewer_prompt() -> str: ...      # Russell
def worker_prompt() -> str: ...
def heartbeat_prompt() -> str: ...
def triage_prompt() -> str: ...
```

Registration pattern (from `core_agent_profiles/plugin.py`):

```python
plugin = PollyPMPlugin(
    name="core_agent_profiles",
    capabilities=(
        Capability(kind="agent_profile", name="polly"),
        Capability(kind="agent_profile", name="russell"),
        Capability(kind="agent_profile", name="heartbeat"),
        Capability(kind="agent_profile", name="worker"),
        Capability(kind="agent_profile", name="triage"),
    ),
    agent_profiles={
        "polly": lambda: StaticPromptProfile(name="polly", prompt=polly_prompt()),
        "russell": lambda: StaticPromptProfile(name="russell", prompt=reviewer_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=heartbeat_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=worker_prompt()),
        "triage": lambda: StaticPromptProfile(name="triage", prompt=triage_prompt()),
    },
)
```

## File Structure

- `src/pollypm/agent_profiles/__init__.py` — registry (`get_agent_profile(name)`).
- `src/pollypm/agent_profiles/base.py` — `AgentProfile` protocol, `AgentProfileContext`.
- `src/pollypm/plugins_builtin/core_agent_profiles/profiles.py` — all five core persona prompts.
- `src/pollypm/plugins_builtin/core_agent_profiles/profiles/polly-operator-guide.md` — Polly's full operator guide (loaded by `polly_prompt`).
- `src/pollypm/plugins_builtin/core_agent_profiles/plugin.py` — registration.
- `src/pollypm/memory_prompts.py` — `build_worker_protocol_injection`, `build_memory_injection`.
- `src/pollypm/rules.py` — `render_session_manifest` (session-scoped rules).
- `src/pollypm/plugins_builtin/advisor/profiles/advisor.md` — advisor persona.
- `src/pollypm/plugins_builtin/downtime/profiles/explorer.md` — downtime explorer.
- `src/pollypm/plugins_builtin/morning_briefing/profiles/herald.md` — herald briefing persona.
- `src/pollypm/plugins_builtin/project_planning/profiles/architect.md`, `critic_*.md` — architect + 5 critics.

## Implementation Details

- **Polly.** Delegates implementation; never writes code or ships artifacts. `polly_prompt()` hardcodes the identity block plus a reference to `polly-operator-guide.md` for the full operator manual. The operator state brief (`_render_operator_state_brief`) caps at 6 projects × 3 items per project to keep the prompt compact.
- **Russell (reviewer).** The review persona. Lives in `reviewer_prompt()`. Reject first pass by default — the bar is "holy shit, that is done."
- **Worker.** The implementer. `worker_prompt()` emphasizes the task lifecycle: `pm task next → claim → implement → done`. Uses `pm notify` for blocks. The worker-protocol injection (`build_worker_protocol_injection`) always prepends the canonical worker guide so a fresh worker always sees it regardless of persona source.
- **Heartbeat.** The supervisor persona — different from the rail-layer `Heartbeat` class. `heartbeat_prompt()` tells the CLI agent its job is monitoring, not implementation; it may not write code or edit files. The heartbeat agent still exists as a supervised session in the default `pollypm.dev.toml`, but most of its responsibilities have moved to the sealed `HeartbeatRail`.
- **Per-project persona names.** `[projects.<key>].persona_name` lets a project override the worker's name (e.g. "Ada", "Bea"). The worker prompt includes a note that the user can change the name conversationally and the agent should patch `.pollypm/config/project.toml` to persist it.
- **Prompt assembly gotchas.** `config._normalize_session_prompt` rewrites legacy full-text prompts in user configs so they pick up the up-to-date persona code. Do not copy-paste persona text into user configs — use `agent_profile = "polly"` instead.
- **Plugin modules and dataclasses.** Plugin personas that use markdown files on disk are implemented as plain `__slots__` classes, not `@dataclass(slots=True)`, because plugin modules are loaded via `importlib.util.spec_from_file_location` without a `sys.modules` entry — Python 3.14's dataclass implementation trips on `cls.__module__` in that case.

## Related Docs

- [modules/memory.md](../modules/memory.md) — memory + worker-protocol injection.
- [features/tasks-and-flows.md](tasks-and-flows.md) — the lifecycle workers execute.
- [plugins/core-agent-profiles.md](../plugins/core-agent-profiles.md) — plugin wiring.
- [features/inbox-and-notify.md](inbox-and-notify.md) — canonical escalation channel used by every persona.
