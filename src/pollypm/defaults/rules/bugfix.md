Description: How to fix bugs thoroughly
Trigger: when fixing bugs or debugging

# Bugfix Rule

## Mindset
A bug fix is not done when the error goes away. It's done when you understand the root cause, fixed it, tested it, and verified no regressions.

## Process
1. **Reproduce first.** Before writing any code, reproduce the bug. If you can't reproduce it, you don't understand it.
2. **Find the root cause.** Don't patch symptoms. Read the error, trace the call chain, understand WHY it broke.
3. **Write a test that fails.** Before fixing, write a test that demonstrates the bug. This prevents regressions.
4. **Fix the root cause.** The smallest change that addresses the actual problem. Not a workaround.
5. **Run the full test suite.** Not just the test you wrote. The entire suite. Report the result.
6. **Verify interactively.** If the bug was user-visible, verify the fix as a user would.
7. **Commit with a clear message.** "fix: <what was wrong and why>" — not "fix bug."

## Anti-patterns
- "It works on my end" — verify in the actual environment
- Disabling the failing test instead of fixing the code
- Adding try/except to hide the error
- "I'll add the test later"
