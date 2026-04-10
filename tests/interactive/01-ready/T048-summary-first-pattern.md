# T048: Summary-First Pattern in All Generated Docs

**Spec:** v1/08-project-state-and-memory
**Area:** Documentation
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that all generated documentation follows the summary-first pattern: each document begins with a concise summary before detailed content.

## Prerequisites
- A project exists with generated documentation (at least 3-4 doc files)
- The docs directory is populated

## Steps
1. List all generated documentation files: `ls .pollypm/docs/` (or equivalent).
2. For each document, read the first 20 lines to check for the summary-first pattern:
   - `head -20 .pollypm/docs/overview.md` — should start with a summary paragraph
   - `head -20 .pollypm/docs/architecture.md` — should start with a summary
   - `head -20 .pollypm/docs/timeline.md` — should start with a summary
3. Verify each document has a clear summary section at the top (e.g., under a "## Summary" heading or as the first paragraph before any detail headings).
4. Verify the summary is concise (1-3 paragraphs, not the entire document).
5. Verify the detailed content follows after the summary (not interspersed).
6. Check at least 5 different document files for the pattern.
7. If any document violates the pattern (starts with details, no summary, or summary buried in the middle), note it as a failure.
8. Verify the summary contains enough information to understand the document's purpose without reading the details.
9. Check that the summary uses plain language and avoids jargon where possible.
10. Count the documents that follow the pattern vs. those that don't. All should follow it.

## Expected Results
- Every generated document begins with a summary
- Summaries are concise (1-3 paragraphs)
- Detailed content follows the summary
- Summaries are self-contained (understandable without reading details)
- 100% of generated docs follow the summary-first pattern

## Log
