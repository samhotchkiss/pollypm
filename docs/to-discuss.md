# To Discuss with Sam

Items that need human input or decision before proceeding.

## Open Questions

1. **Russell's account**: I assigned Russell to `claude_s_swh_me` (your personal Claude account). Should he use a different account, or is sharing with you fine? He'll consume tokens from that subscription.

2. **Deploy step in review**: Should deploy be part of the worker's "done" signal (worker deploys before signaling done), or should Russell trigger deploy after approving? The current flow has no deploy step — the worker just commits.

3. **Human approval UX**: For user-review flow tasks, how should the approval notification look in the cockpit? Options: (a) inbox message with approve/reject buttons, (b) task detail view with approve/reject actions, (c) both.

4. **Project grouping in rail**: You want active projects (task in last 24h) above inactive ones. Should new projects with zero tasks also appear in the "active" group, or only once they have their first task?

## Issues Found During Testing

- `docs/project-overview.md` is 142MB — added to .gitignore. Needs investigation on what's bloating it.
- Operator session prompt is baked into pollypm.toml (not just the profile) — updating builtin.py doesn't fully update the operator prompt without editing the TOML too.
