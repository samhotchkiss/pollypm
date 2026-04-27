# Release Verification Gate

Source: implements GitHub issue #889.

This document specifies the launch-hardening release verification
gate. Code home: `src/pollypm/release_gate.py`. Test home:
`tests/test_release_gate.py`.

## Why a gate

The pre-launch audit (`docs/launch-issue-audit-2026-04-27.md` §8)
identifies the recurring shape of process failures:

* `#395`/`#501`/`#505`/`#511`/`#513`/`#515` — issues closed
  against local branch state and reopened after `origin/main`
  comparison.
* `#840`/`#831`/`#829`/`#826`/`#820` — narrow unit tests
  passed; the bug reproduced after a cockpit restart.
* `#821`→`#514`, `#820`→`#799`, `#819`→`#792` — one-day
  cockpit regressions in launch-critical surfaces.
* `#709` — main red with 12 failures and 10 errors.

The gate is the structural fix — it consolidates the
verification rules into a single set of typed, named predicates
that the release process consults before tagging.

## Gate model

A `Gate` is a callable returning a `GateResult` with:

* `name` — stable identifier.
* `passed` — boolean.
* `severity` — `BLOCKING` or `WARNING`.
* `summary` / `detail` — human-readable explanation.

A `ReleaseReport` aggregates results and exposes `blocked`
(any blocking failure) and `warnings` / `failures`. The
rendered report is designed for CI log readability with
`[PASS] / [FAIL] / [WARN]` tags.

## Default gates

Current set:

| Gate                                  | Severity | Source                              |
| ------------------------------------- | -------- | ----------------------------------- |
| `gate_main_branch_green`              | BLOCKING | `git rev-parse origin/main` compare |
| `gate_cockpit_interaction_audit_clean`| BLOCKING | `cockpit_interaction.REGISTRY.audit`|
| `gate_signal_routing_emitters_migrated`| WARNING | `signal_routing.missing_routed_emitters` |

The signal-routing-emitters gate is a warning rather than
blocking until the work_service / supervisor_alerts /
heartbeat emitters migrate to `SignalEnvelope`. Once that
migration ships, flip its severity in `release_gate.py`.

## Issue-closure metadata schema

`parse_closure_comment(text)` extracts structured metadata from
a free-form GitHub close comment. Required keys:

* `commit` — git hash (validated by regex)
* `branch` — branch / ref verified
* `command` — command(s) run
* `fresh_restart` — yes / no for cockpit / process restart

`closure_comment_complete(text)` returns
`(complete, missing_keys)`. The `pm` close-issue helper (or any
GitHub bot) consults this before allowing the close to proceed.
A close comment that fails this check is rejected with the
exact list of missing keys — that is the structural answer to
the audit's local-branch closure pattern.

## CLI entrypoint

```python
from pollypm.release_gate import run_release_gate

report = run_release_gate()
print(report.render())
sys.exit(1 if report.blocked else 0)
```

`scripts/release_burnin.py` and the GitHub release workflow
both call this. Failures are non-recoverable — the gate is the
last barrier before a tag, so a failing gate must abort the
release.

## Adding a new gate

1. Write a `def gate_my_new_check() -> GateResult:`. Make it
   pure — gates run in isolation and exceptions are caught.
2. Decide `BLOCKING` vs `WARNING`. Default to `WARNING` for
   migration-in-progress invariants; promote to `BLOCKING`
   when the migration is complete.
3. Add to `DEFAULT_GATES` in `release_gate.py`.
4. Update this document with the new row.

## What the gate does NOT do

* It does not run the test suite. CI does that. The gate
  inspects the result of those runs (TODO: surface CI status
  via a future `gate_ci_main_green` once we standardize on a
  CI platform).
* It does not update issues. It only verifies that the local
  state reflects what closure metadata claims.
* It does not block a developer's local workflow — it is
  designed to be invoked at release-tag time, not on every
  commit.

*Last updated: 2026-04-27.*
