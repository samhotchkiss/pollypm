# 0018 Worker Lane Kickoff And Project Launch Polish

## Goal

Make it easier for Polly to kick off a concrete worker lane for a selected project, with clearer prompts, tighter default issue selection, and less chance of falling into generic placeholder work.

## Scope

- improve project-to-worker kickoff from the control room
- tighten default worker prompts and issue targeting
- reduce generic placeholder launches
- make active worker selection and resume behavior clearer in tmux

## Acceptance Criteria

- starting work from Polly or the control room yields a concrete, scoped worker lane
- worker launches do not fall back to vague placeholder tasks
- project launch behavior is clear and predictable in the live tmux flow
