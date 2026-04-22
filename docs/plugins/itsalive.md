**Last Verified:** 2026-04-22

## Summary

`itsalive` is the dedicated plugin for the itsalive.co deploy wrapper. It appends deploy instructions to the Polly / worker / heartbeat persona prompts, registers a sweep roster entry so pending deploys complete once email verification lands, and hooks into the session-after-launch observer so newly-launched sessions know about the wrapper.

Feature-level details (pending-state shape, API surface, CLI) live in [features/itsalive.md](../features/itsalive.md). This doc covers plugin wiring only.

## Core Contracts

The plugin composes new prompts rather than registering new personas:

```python
# src/pollypm/plugins_builtin/itsalive/plugin.py
def _polly_prompt() -> str:
    return f"{polly_prompt()}\n\n...\n{build_deploy_instructions()}"

def _worker_prompt() -> str:
    return f"{worker_prompt()}\n\n...\n{build_deploy_instructions()}"

def _heartbeat_prompt() -> str:
    return f"{heartbeat_prompt()}\n\n...\n{build_deploy_instructions()}"
```

Hooks:

- `session.after_launch` observer (`_on_session_after_launch`) — logs when an itsalive-aware session starts.
- Sweep roster entry — periodic `sweep_pending_deploys` against every registered project.

## File Structure

- `src/pollypm/plugins_builtin/itsalive/plugin.py` — registration + prompt augmentation + sweep.
- `src/pollypm/plugins_builtin/itsalive/pollypm-plugin.toml` — manifest.
- `src/pollypm/itsalive.py` — the module with `build_deploy_instructions`, `sweep_pending_deploys`, deploy flow.
- `src/pollypm/messaging.py` — `notify_deploy_verification_required` / `_expired` / `_complete` helpers.

## Implementation Details

- **Persona override by composition.** Rather than replacing `polly` / `worker` / `heartbeat` profiles, the plugin registers additional profiles (`polly`, `worker`, `heartbeat`) whose prompt text combines the core prompt + itsalive addendum. The registry resolves the last-registered factory, so loading this plugin after `core_agent_profiles` effectively augments the prompts.
- **Sweep scope.** `_resolve_project_root(payload)` picks the project the sweep targets. The sweep is idempotent — completing a deploy removes its pending file; expired deploys get an expiration notification before cleanup.
- **No email loop in workers.** The plugin's core promise: a worker never blocks on email verification. The cockpit shows pending deploys; the sweeper completes them.

## Related Docs

- [features/itsalive.md](../features/itsalive.md) — feature-level spec.
- [features/agent-personas.md](../features/agent-personas.md) — persona composition pattern.
- [plugins/magic.md](magic.md) — compatibility alias that references the same instructions.
