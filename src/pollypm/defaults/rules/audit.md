Description: How to audit code and deliver actionable findings
Trigger: when reviewing or auditing existing code

# Audit Rule

## Mindset
An audit is not a list of complaints. It's a prioritized, actionable report that helps the team make the codebase better. Every finding should have a specific location, a clear description, and a suggested fix.

## Process
1. **Read before judging.** Understand the code's intent, not just its implementation. Read surrounding files for context.
2. **Check systematically.** Don't just grep for patterns — trace actual call paths:
   - Correctness: does it do what it claims?
   - Security: input validation, injection risks, auth boundaries
   - Performance: N+1 queries, unnecessary I/O, missing caching
   - Clarity: would a new developer understand this?
   - Coverage: are the critical paths tested?
3. **Verify claims.** If the code says "thread-safe" — verify it. If it says "handles errors" — check the error paths.
4. **Record with specifics.** File path, line number, what's wrong, why it matters, suggested fix.
5. **Prioritize by impact.** Critical (data loss, security) > High (broken feature) > Medium (reliability) > Low (style).

## Deliverable
Create a structured report file (not chat output). Use the visual-explainer magic for HTML reports if available. Include:
- Executive summary (3 sentences)
- Findings table (severity, location, description, fix)
- Architecture observations
- Recommended next steps
