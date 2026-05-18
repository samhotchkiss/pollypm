# [CANCELLED] Blocked: itsalive/33 critique cannot mark done

**Critique is complete** — full structured output in:
- `docs/plan/critique_maintainability.json` (persona, scores, preferred_candidate, required_changes, risks, rationale)
- `docs/plan/critique_maintainability.md` (prose)
- Commits `d6a6429` and `b08fd42` on branch `task/itsalive-33`

**Headline finding:** prefer Candidate A (score 8) over C (5) and B (4), with 6 required changes that close A's file-size gap by adding bounded per-module physical extraction, a shared-helpers slot, adapter design folded into the harness, and an ITSALIVE.md snapshot test.

**Blocker:** `pm task done itsalive/33 --actor critic_maintainability --output <envelope>` fails the `output_present` hard gate. Root cause: in `service_transition_manager.node_done` (~lines 1038–1071), `evaluate_gates()` is called BEFORE `work_output` is coerced/persisted; the gate reads `execution.work_output` from the DB, finds `null` on the sole active execution, rejects. `pm task done` does not expose `--skip-gates`. The heartbeat-suggested minimal envelope (`code_change` + 1 file_change artifact) was tried and hits the same gate.

**To verify:** `pm task get-execution itsalive/33 --json` → 1 active execution with `work_output: null`. `pm task validate-advance itsalive/33 --actor critic_maintainability` → output_present hard fail.

**Unstick options (lowest blast radius first):**
1. Manual SQL: `UPDATE work_node_executions SET work_output=<jsonblob> WHERE task_project='itsalive' AND task_number=33 AND status='active'` then re-run `pm task done`.
2. Add `--skip-gates` to `pm task done` CLI (one-line typer.Option + pass-through).
3. Patch `service_transition_manager.node_done` to coerce + persist work_output before `evaluate_gates`.
