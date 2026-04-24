# Release-Fix Packaging Rule

Near release, fixes must be split by rollback boundary. A PR should be easy to
revert without also reverting unrelated product behavior.

## Required split

Use separate PRs or commits for these buckets:

- Task lifecycle and scheduler semantics.
- Session lifecycle and tmux/runtime behavior.
- Cockpit UI-only rendering.
- Inbox/message triage and notification copy.
- Agent prompt/profile text.
- Model registry or provider configuration.
- Migration/storage schema changes.

If a fix legitimately crosses buckets, the PR description must name the shared
invariant and explain why one atomic change is safer than separate changes.

## Review Checklist

Every release-fix PR must include:

- The subsystem bucket it belongs to.
- The GitHub issue numbers it closes.
- Targeted tests and any live tmux verification performed.
- A rollback note describing what user-visible behavior reverts.
- A statement that plugin boundaries were preserved, or the explicit contract
  that was changed.

Mixed-scope PRs are not blocked automatically, but reviewers should ask for a
split unless the author documents the coupling above.
