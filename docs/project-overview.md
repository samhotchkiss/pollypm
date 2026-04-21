# Project Overview

## Summary
- PollyPM is a tmux-first control plane for people running multiple AI coding sessions in parallel without giving up visibility or manual control.
- The system is a modular monolith with replaceable provider, runtime, storage, scheduler/heartbeat, and cockpit-module boundaries.
- The core v1 loop is: operator request -> task planning/work routing -> per-task worker session -> review/handoff -> persistent state and recoverable transcripts.
- This document stays deliberately short; raw extracted knowledge lives under `.pollypm/knowledge/`, and deeper references live in the docs listed below.

## What PollyPM Is
PollyPM manages real terminal-native coding sessions. It launches operator and worker sessions in `tmux`, tracks them through a shared SQLite-backed state layer, and gives the operator a CLI and Textual cockpit for visibility, approval, and intervention.

The project is optimized for replaceability inside one local system:

- providers are plugins
- runtimes are plugins
- recurring jobs and heartbeat sweeps are plugins
- cockpit panels are split modules behind stable routes
- task flow lives behind the work-service boundary

## Core Runtime
- `src/pollypm/supervisor.py` owns session lifecycle, health, recovery, and failover orchestration.
- `src/pollypm/work/` owns task state, flow transitions, review gates, and worker-session routing.
- `src/pollypm/storage/state.py` and the Store layer own durable local state.
- `src/pollypm/cockpit_*.py` modules power the Textual cockpit.
- `src/pollypm/acct/` and `src/pollypm/providers/` define the provider substrate.
- `src/pollypm/runtimes/` defines execution environments.

## Current Operator Workflow
1. The operator asks Polly to plan or execute work.
2. Polly creates or advances tasks in the work service.
3. A per-task worker session is provisioned in an isolated worktree.
4. The worker hands off with structured output.
5. Review happens through the task flow and, when required, explicit human approval.
6. The cockpit and CLI reflect the current state from shared storage.

## Replaceable Boundaries
- Provider adapters define login, launch, resume, and usage-probe behavior.
- Runtime adapters define how sessions are executed.
- Session services define how panes/sessions are provisioned and resumed.
- Work-service APIs define task inputs, outputs, and transitions.
- Cockpit modules define independently testable UI surfaces.

## Documentation Boundaries
- Front-door docs live in `docs/` and should remain concise.
- Deep reference/spec material lives in `docs/v1/`, `docs/internals/`, and module-specific specs.
- Raw extracted knowledge belongs in `.pollypm/knowledge/`.

## Read Next
- [README.md](../README.md) for the current architecture and module map
- [docs/getting-started.md](getting-started.md) for first-run/operator flow
- [docs/architecture.md](architecture.md) for the higher-level system map
- [docs/worker-guide.md](worker-guide.md) for worker lifecycle and handoff rules
- [docs/work-service-spec.md](work-service-spec.md) for task/flow semantics
