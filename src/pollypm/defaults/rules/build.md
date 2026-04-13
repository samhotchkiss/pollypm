Description: How to build features that impress
Trigger: when building new features or components

# Build Rule

## Mindset
The standard is not "it works" — it's "holy shit, that's done." Every feature should be complete, tested, documented, and polished before you report it.

## Process
1. **Understand before building.** Read existing code. Search for related implementations. Don't reinvent what exists.
2. **Break into testable increments.** Each commit should be a working state. No "WIP" commits.
3. **Write tests alongside code.** Not after. Not "I'll add them later." With each increment.
4. **Verify as a user would.** Run the actual feature. Check edge cases. If it's a UI, look at it. If it's a CLI, use it.
5. **Commit frequently.** Small, focused commits with clear messages. Push after each meaningful change.
6. **Document what's not obvious.** If someone reading the code would wonder "why?" — add a comment or doc.

## Completion Checklist
- [ ] Feature works as specified
- [ ] Tests pass (run the actual test suite, don't assume)
- [ ] Edge cases handled
- [ ] No regressions (run full suite, not just your tests)
- [ ] Committed and pushed
- [ ] If the user asked to "deploy" — deploy it and verify the live version
