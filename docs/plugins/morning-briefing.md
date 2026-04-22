**Last Verified:** 2026-04-22

## Summary

`morning_briefing` delivers one inbox message per local day at the configured hour (default 6 AM) summarizing yesterday's cross-project activity and today's priorities. Fires on an hourly tick; the handler internally gates on the configured briefing hour and on already-briefed-today so the effect is exactly-once-per-day, resilient to missed ticks and process restarts. An every-6h sweep auto-closes un-pinned briefings older than 24 hours.

Touch this plugin when changing the briefing tick gate, the herald persona, the sweep window, or adding a new data source. The gather / synthesize / emit logic is intentionally behind `fire_briefing` so individual upgrades can land without touching the roster wiring.

## Core Contracts

Registered:

- `herald` agent profile from `profiles/herald.md`.
- `briefing.tick` handler (max_attempts=1, timeout=60s).
- `briefing.sweep` handler (max_attempts=1, timeout=30s).
- Roster: `@every 1h` → `briefing.tick`; `@every 6h` → `briefing.sweep`. Both have matching `dedupe_key`.

Config (`pollypm.toml`):

```toml
[briefing]
hour = 6                    # local hour to fire
```

`pm briefing` CLI lives in `plugins_builtin/morning_briefing/cli.py`.

## File Structure

- `src/pollypm/plugins_builtin/morning_briefing/plugin.py` — registration + `_herald_prompt`.
- `src/pollypm/plugins_builtin/morning_briefing/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/morning_briefing/profiles/herald.md` — persona.
- `src/pollypm/plugins_builtin/morning_briefing/settings.py` — `[briefing]` parsing.
- `src/pollypm/plugins_builtin/morning_briefing/state.py` — persistent tick state.
- `src/pollypm/plugins_builtin/morning_briefing/inbox.py` — `briefing_sweep_handler`.
- `src/pollypm/plugins_builtin/morning_briefing/handlers/briefing_tick.py` — `briefing_tick_handler`, `fire_briefing`.
- `src/pollypm/plugins_builtin/morning_briefing/flows/morning_briefing.yaml` — briefing flow template.
- `src/pollypm/plugins_builtin/morning_briefing/cli.py` — `pm briefing`.

## Implementation Details

- **Hot re-read of the persona.** `_herald_prompt()` re-reads `herald.md` every call so edits during development take effect without restarting the host. The file is tiny — the re-read cost is negligible.
- **Fallback persona.** If `herald.md` is missing, `_herald_prompt()` returns a minimal stub so the plugin still loads and the roster entry stays intact.
- **Tick gating.** The tick fires hourly but only emits when (a) the current local hour matches the configured briefing hour and (b) no briefing has been emitted today yet. Both checks live in the handler, so cadence changes don't require register-time reasoning.
- **Sweep cadence.** Every 6h — anything more frequent would be wasted work since the briefing fires once per day.

## Related Docs

- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — where the briefing lands.
- [features/agent-personas.md](../features/agent-personas.md) — herald persona.
