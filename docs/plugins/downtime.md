**Last Verified:** 2026-04-22

## Summary

`downtime` is the idle-budget exploration lane. When PollyPM has unused LLM capacity, the downtime tick (default `@every 12h`) kicks off autonomous exploration (specs, speculative builds, doc audits, security scans, alternative approaches). **Load-bearing rule: nothing produced in downtime auto-deploys.** Every exploration ends with an inbox message awaiting explicit user approval.

Touch this plugin when changing cadence, the explorer persona, the exploration flow, or adding a new gate. Do not loosen the never-auto-deploy validator — it is the safety guarantee.

## Core Contracts

Registered:

- `explorer` agent profile (from `profiles/explorer.md`, re-read on every invocation).
- `downtime.tick` job handler (max_attempts=1, timeout=60s).
- Roster: configured cadence (default `@every 12h`) → `downtime.tick` with `dedupe_key="downtime.tick"`.

Config (`pollypm.toml`):

```toml
[downtime]
cadence = "@every 12h"
```

Flow template: `downtime_explore.yaml` — validated via `assert_downtime_flow_shape` at plugin init.

`pm downtime` CLI lives in `plugins_builtin/downtime/cli.py`.

## File Structure

- `src/pollypm/plugins_builtin/downtime/plugin.py` — registration + cadence resolution + bundled-flow validator.
- `src/pollypm/plugins_builtin/downtime/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/downtime/profiles/explorer.md` — persona.
- `src/pollypm/plugins_builtin/downtime/settings.py` — `[downtime]` parsing, `DEFAULT_CADENCE`.
- `src/pollypm/plugins_builtin/downtime/state.py` — persistent tick state.
- `src/pollypm/plugins_builtin/downtime/flow_validator.py` — `assert_downtime_flow_shape`, `DowntimeFlowValidationError`.
- `src/pollypm/plugins_builtin/downtime/flows/downtime_explore.yaml` — the shipped flow.
- `src/pollypm/plugins_builtin/downtime/handlers/downtime_tick.py` — tick body.
- `src/pollypm/plugins_builtin/downtime/gates/` — downtime-specific gates.
- `src/pollypm/plugins_builtin/downtime/cli.py` — `pm downtime`.

## Implementation Details

- **Flow validator.** `assert_downtime_flow_shape(flow)` enforces the spec §10 never-auto-deploy rule at plugin init: the flow must end at a human-review node, no auto-ship terminal. `_validate_bundled_flow()` re-runs this on the shipped YAML so a bad edit fails loud.
- **Persona hot-reload.** `_MarkdownPromptProfile.build_prompt` re-reads `explorer.md` on every call so persona edits show up live.
- **`max_attempts=1`.** 12h cadence + per-tick guards means retries would cause double-scheduling. A failed tick will simply fire on the next cadence.
- **Cadence-reload lifecycle.** `_register_roster` reads config at bootstrap; rail restart required to pick up cadence changes.

## Related Docs

- [features/tasks-and-flows.md](../features/tasks-and-flows.md) — flow validation and gates.
- [features/agent-personas.md](../features/agent-personas.md) — explorer persona shape.
- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — where exploration outputs land.
