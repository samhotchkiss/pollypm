**Last Verified:** 2026-04-22

## Summary

`advisor` runs a quiet 30-minute loop that reviews recent project activity against the plan and only emits an inbox insight when something structurally worth saying shows up. Silent-by-default — the persona itself is the quality filter. Also runs a 12h autoclose sweep to close out stale advisor inbox items.

Touch this plugin when changing the cadence knob, the autoclose window, the persona prompt, or adding a new advisor gate. Do not add new advisor modes — spin a sibling plugin.

## Core Contracts

Registered:

- `advisor` agent profile (loaded from `profiles/advisor.md`).
- `advisor.tick` job handler, max_attempts=1, timeout=60s.
- `advisor.autoclose` job handler, max_attempts=1, timeout=30s.
- Roster: cadence (default `@every 30m`) → `advisor.tick`; `@every 12h` → `advisor.autoclose`. Both have `dedupe_key` matching the handler name.

Config (`pollypm.toml`):

```toml
[advisor]
cadence = "@every 30m"
```

`pm advisor` CLI is wired via `plugins_builtin/advisor/cli/advisor_cli.py`.

## File Structure

- `src/pollypm/plugins_builtin/advisor/plugin.py` — registration, cadence resolution.
- `src/pollypm/plugins_builtin/advisor/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/advisor/profiles/advisor.md` — persona prompt.
- `src/pollypm/plugins_builtin/advisor/settings.py` — `load_advisor_settings`, `DEFAULT_CADENCE`.
- `src/pollypm/plugins_builtin/advisor/state.py` — advisor run state persistence.
- `src/pollypm/plugins_builtin/advisor/inbox.py` — inbox item assembly.
- `src/pollypm/plugins_builtin/advisor/handlers/advisor_tick.py` — tick body.
- `src/pollypm/plugins_builtin/advisor/handlers/autoclose.py` — autoclose body.
- `src/pollypm/plugins_builtin/advisor/flows/advisor_review.yaml` — review flow template.
- `src/pollypm/plugins_builtin/advisor/cli/advisor_cli.py` — `pm advisor` commands.

## Implementation Details

- **`_MarkdownPromptProfile`.** Plain class with `__slots__ = ("name", "path")`, not `@dataclass(slots=True)` — plugin modules are loaded via `importlib.util.spec_from_file_location` without a `sys.modules` entry, which trips dataclass's `cls.__module__` lookup under Python 3.14.
- **Cadence reload.** `_resolve_cadence()` reads config at `register_roster` time only. A cadence change in `pollypm.toml` requires a rail restart — see `plugin.py:77`.
- **Silent-by-default.** The tick runs unconditionally; the persona decides whether to emit. This design keeps the quality bar in one place (the prompt) rather than in per-metric gates that drift from user taste.
- **Autoclose.** `@every 12h` — enough to reclaim stale items without churning the inbox.

## Related Docs

- [features/agent-personas.md](../features/agent-personas.md) — persona registration pattern.
- [modules/heartbeat.md](../modules/heartbeat.md) — roster + tick.
