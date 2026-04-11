# Decisions

## Summary
Key architectural and technical decisions for PollyPM.

## Decisions
- PollyPM: Agents run natively in isolated tmux windows, not as subprocess abstractions
- PollyPM: Operator can always drop into any window and take direct control
- PollyPM: Recovery based on observable logs, git state, and checkpoints (not hidden model state)
- Use chronological event walk for LLM extraction rather than random access
- Development started on 2026-04-01 with foundational decisions on tech stack and coding conventions
- The project was initiated on April 1, 2026, with core technical decisions made immediately
- Development began in early April 2026 with foundational architectural decisions
