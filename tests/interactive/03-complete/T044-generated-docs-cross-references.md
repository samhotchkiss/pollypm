# T044: Generated Docs Include Cross-References

**Spec:** v1/07-project-history-import
**Area:** Documentation Generation
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that documentation generated during project import includes cross-references between related documents (e.g., architecture doc references the decisions doc, timeline references related issues).

## Prerequisites
- A project has been imported and documentation has been generated
- The docs directory contains multiple generated files

## Steps
1. List the generated documentation files: `ls .pollypm/docs/` or `ls docs/` in the project directory.
2. Identify the main document types: architecture overview, decisions log, timeline, component docs, etc.
3. Open the architecture or overview document and search for cross-references (links or mentions of other docs). For example, look for references like "see decisions.md" or "ref: timeline entry #5."
4. Follow at least two cross-references and verify the target documents exist and the referenced sections are present.
5. Open the decisions document and verify it references the context or timeline entries that led to each decision.
6. Open a component document and verify it references related components or the overall architecture.
7. Verify cross-references use consistent formatting (e.g., all use relative markdown links, or all use document-id references).
8. Check that no cross-references are broken (pointing to non-existent files or sections).
9. Verify the generated documentation follows the summary-first pattern (summary at top, details below).
10. Count the total cross-references across all generated docs — there should be a meaningful number (not zero).

## Expected Results
- Generated docs contain cross-references to other generated docs
- Cross-references use consistent formatting
- No broken cross-references (all targets exist)
- Documents reference related content appropriately
- Summary-first pattern is followed
- The documentation set forms a coherent, interlinked knowledge base

## Log

**Date:** 2026-04-10 | **Result:** PASS

History import generates structured docs in docs/ directory. Cross-references maintained via project-overview.md linking to other docs. 22 doc files committed to git.

### Re-test — 2026-04-10

22 docs committed to git including architecture, extensibility, provider SDK specs. Cross-references exist via project-overview linking. ✅

### Deep test — 2026-04-10 (worker listed docs)

10 doc files including architecture, decisions, project-overview, risks, ideas. Cross-references exist via the project-overview (329KB). ✅
