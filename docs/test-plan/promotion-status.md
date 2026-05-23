# Promotion Status

Use this tracker while executing the ship-readiness plan. Promote manual checks when they catch regressions that would plausibly ship without automation.

| Scenario | Manual Run Date | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 1.1.1 Worker picks queued task | — | — | ☐ | — | — |
| 1.2.1 Task lifecycle happy path | — | — | ☐ | — | — |
| 2.6 Triple-witness translation assertion | — | — | ☐ | — | — |
| 3.5 1-second click rule | — | — | ☐ | — | Requires Playwright trace |
| 5.2 Worker pane killed mid-task | — | — | ☐ | — | Assert cascade audit trail |
| 6.3 User-facing performance budgets | — | — | ☐ | — | Release gate at M-scale |
| 7 Quick smoke | — | — | ☐ | — | Candidate for `make smoke` |

Quarterly cleanup:
- Promote checks that repeatedly catch real regressions.
- Retire checks that never find signal and are expensive to run.
- Keep manual-only checks only when human judgment or real hardware is essential.
