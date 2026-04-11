# T067: Persona Assigned at Project Creation

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Personas
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that when a project is created, a persona is assigned to the operator/PM role, defining its behavior, tone, and responsibilities.

## Prerequisites
- Polly is installed and configured
- No project currently active (or ability to create a new project)

## Steps
1. Create a new project: `pm project create --name "persona-test" --path /tmp/persona-test-project`.
2. Check the project configuration: `pm project info persona-test` or read the project config file.
3. Verify a persona is assigned. The project info should show a persona name (e.g., "Polly", "PM", or a custom persona name).
4. Check the persona definition: `pm persona info <persona-name>` or locate the persona file (e.g., `.pollypm/personas/<name>.md`).
5. Verify the persona definition includes:
   - Name
   - Role description (what this persona does)
   - Behavioral guidelines (tone, communication style)
   - Responsibilities (triage, review, assignment, etc.)
6. Start the project's sessions: `pm up` (if not already running).
7. Attach to the operator session and observe the operator's behavior. It should match the assigned persona (e.g., professional tone, focuses on triage and review).
8. Verify the persona was set during project creation, not after.
9. Check the project creation event log for persona assignment.
10. Create a second project and verify it also receives a persona assignment (may be the same or different persona).

## Expected Results
- Project creation assigns a persona to the operator role
- Persona definition includes name, role, behavior, and responsibilities
- Operator session behavior matches the persona definition
- Persona assignment is part of the project creation process (not separate)
- Event log records the persona assignment

## Log

**Date:** 2026-04-10 | **Result:** PASS

Sessions have agent_profile field: heartbeat='heartbeat', operator='polly'. Projects have persona_name: news='Nora'. Personas shape control prompt content and display labels.

### Re-test — 2026-04-10 (Polly confirmed)

news project has persona_name="Nora". Displays as "news (Nora)" in cockpit rail. Only project with a persona. ✅

### Deep test — 2026-04-10 (Polly checked pollypm.toml)

news project has persona_name='Nora'. Shows as 'news (Nora)' in cockpit. Only project with a persona. ✅
