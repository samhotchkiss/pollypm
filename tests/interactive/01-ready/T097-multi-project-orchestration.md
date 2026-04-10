# T097: Multi-Project Orchestration: Two Projects Running Simultaneously

**Spec:** End-to-End Workflows
**Area:** Multi-Project
**Priority:** P1
**Duration:** 30 minutes

## Objective
Verify that Polly can manage two projects simultaneously, with separate sessions, issues, and workers for each project, without cross-contamination.

## Prerequisites
- Polly is installed with at least two accounts (or enough for two projects)
- Two separate git repositories/directories for projects
- `pm down` has been run (clean slate)

## Steps
1. Create Project A: `pm project create --name "project-alpha" --path /tmp/project-alpha`.
2. Create Project B: `pm project create --name "project-beta" --path /tmp/project-beta`.
3. Verify both projects exist: `pm project list`.
4. Start both projects: `pm up --project project-alpha` and `pm up --project project-beta` (or `pm up --all`).
5. Run `pm status` and verify sessions are running for both projects. Each project should have its own operator and worker sessions.
6. Create an issue for Project A: `pm issue create --project project-alpha --title "Alpha task" --body "Create alpha.txt with 'This is Project Alpha'"`.
7. Create an issue for Project B: `pm issue create --project project-beta --title "Beta task" --body "Create beta.txt with 'This is Project Beta'"`.
8. Move both issues to ready and wait for workers to pick them up.
9. Verify each worker is working on the correct project:
   - Project A's worker should be working in /tmp/project-alpha/
   - Project B's worker should be working in /tmp/project-beta/
10. Wait for both tasks to complete (up to 15 minutes).
11. Verify Project A's file: `cat /tmp/project-alpha/alpha.txt` — should contain "This is Project Alpha."
12. Verify Project B's file: `cat /tmp/project-beta/beta.txt` — should contain "This is Project Beta."
13. Verify NO cross-contamination: `ls /tmp/project-alpha/beta.txt` should fail, and `ls /tmp/project-beta/alpha.txt` should fail.
14. Run `pm down` and clean up.

## Expected Results
- Two projects run simultaneously without interference
- Each project has its own sessions, issues, and workers
- Workers operate in the correct project directory
- No cross-contamination between projects (files, issues, state)
- Both projects complete their tasks independently
- `pm status` shows sessions for both projects

## Log
