# North Star

**Last Verified:** 2026-04-22

## Why PollyPM exists

Running AI coding agents is cheap. Keeping them moving — unblocked, reviewed, recovered from crashes, routed around rate limits, and aimed at work that matters — is expensive human attention. PollyPM replaces that human attention with a supervisor: a tmux-first control plane that orchestrates a *team* of agents against local repositories.

## Mission

**Stop babysitting your AI.** Keep many agents usefully employed against real projects without paying per-token API rates, without losing session state to crashes, and without a human needing to constantly re-shepherd each session.

## Non-negotiable priorities

The project is targeting a **v1 tag on 2026-04-16** (now past), so the operating priorities are:

1. **Stability over features.** Nothing in v1 breaks existing users. Schema migrations, supervisor refactors, and plugin-boundary work all preserve backward compatibility.
2. **Signal over volume.** The cockpit surfaces the *next decision the human needs to make*, not a firehose of events. The inbox is the canonical escalation channel — `pm notify`.
3. **Local-first, subscription-native.** PollyPM drives Claude Code and Codex CLIs directly against the user's existing paid accounts. No API-metered middle layer, no cloud dependency.
4. **Human takeover always works.** Every agent runs in a real tmux pane a human can attach to and type into. Automation yields via the lease model.
5. **Seams are replaceable.** Providers, runtimes, session services, agent profiles, storage backends, recovery policy, launch planner, and cockpit rail items are all plugins. The core is a substrate; everything else is policy.

## Target users

A solo developer or small team who:

- already pays for Claude Code and/or Codex subscriptions
- works on multiple projects locally (a `~/dev` tree, mostly git repos)
- wants concurrent lanes of work without one chat session drowning out the others
- wants review and recovery built in, not bolted on

**Not for:** hosted-SaaS agent users, teams that need multi-machine orchestration, or single-shot copilot use cases.

## Success looks like

- `pm up` attaches to a cockpit where Polly (PM), Russell (review), and per-task workers are already managed and self-recovering.
- A rate-limited account fails over to another without user intervention.
- A stuck worker is nudged, then reset, then relaunched — or escalated to the human only when the ladder runs out.
- `pm notify` is the only channel the human monitors; agents do not chase the human in chat.
- A day's output is reviewable through `pm inbox` and the cockpit dashboard.

## Operating philosophy

- **Opinionated but pluggable.** Every subsystem ships a strong default. Every subsystem is swappable via the plugin API. Users don't fight the system; they replace the parts they disagree with.
- **Core = substrate.** Tmux, state store, plugin host, heartbeat rail, transcript ingest, accounts, scheduler. Tiny, stable, well-tested.
- **Plugins = policy.** Issue management, documentation, advisor, morning briefing, downtime exploration, memory curation, notifications, and all agent personas live in `plugins_builtin/`.
- **Agents are clients.** Persona prompts carry the behavioral contract; reference material lives in docs and `pm help`, not in every session's context window.

## Key constraints

- Single-host only. No multi-machine orchestration.
- Python 3.13+, SQLite, tmux. No services, no containers required (Docker runtime is optional).
- Providers drive interactive CLIs, not APIs. PollyPM never proxies provider-API calls.
