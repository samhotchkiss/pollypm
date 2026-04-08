# 0002 Operator Kickoff And Resume

## Goal

Make the PM operator behave like a real project manager who kicks off and oversees work sessions, and ensure the PM sessions resume their prior Claude/Codex conversation on restart.

## Implemented

- Strengthened the heartbeat/operator control prompts with explicit PM/PA boundaries.
- Added resume markers for control sessions.
- Wired Claude to resume with `--continue` and Codex to resume with `resume --last` after the first successful launch.
- Applied the stronger control prompt through effective session planning so existing configs benefit too.

## Review Focus

- Does the operator prompt now push kickoff/oversight instead of freelancing implementation work?
- Do control sessions resume cleanly after restart without breaking first launch?

## Validation

- Focused tests pass.
- Full suite pass is required before completion.
