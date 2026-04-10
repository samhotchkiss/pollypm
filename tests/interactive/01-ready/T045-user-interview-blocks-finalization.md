# T045: User Interview Blocks Finalization

**Spec:** v1/07-project-history-import
**Area:** Project Import
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the project import process includes a user interview step that blocks documentation finalization, ensuring human input shapes the final project documentation.

## Prerequisites
- Polly is installed and configured
- A git repository available for import
- Understanding of the import workflow with interview step

## Steps
1. Start a project import: `pm project import <repo-path>` (or equivalent).
2. Observe the import process: it should first analyze the git history and generate draft documentation.
3. At some point, the process should pause and prompt for a user interview. Look for prompts like "Please answer the following questions about your project" or similar.
4. Verify the process is BLOCKED — it should not finalize documentation until the interview is completed.
5. Check `pm project status <project-name>` — it should show a status like "awaiting_interview" or "draft."
6. Answer the interview questions. These may include:
   - What is the primary purpose of this project?
   - What are the key architectural decisions?
   - Who are the main stakeholders?
   - What are the current priorities?
7. After completing the interview, observe the process resume and incorporate your answers into the documentation.
8. Verify the finalized documentation includes content from your interview answers (not just git-derived content).
9. Check `pm project status <project-name>` — it should now show "active" or "finalized."
10. Verify that the generated INSTRUCT.md (or equivalent) includes project context derived from the interview.

## Expected Results
- Import process pauses for user interview before finalizing docs
- Process is genuinely blocked (not just suggesting interview as optional)
- Interview questions are relevant to the project
- Interview answers are incorporated into the final documentation
- Documentation quality is measurably better with interview input
- INSTRUCT.md includes project-specific context from the interview

## Log
