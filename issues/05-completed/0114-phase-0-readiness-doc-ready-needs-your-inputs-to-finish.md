# [CANCELLED] Phase 0 readiness doc ready — needs your inputs to finish

**Task:** samblog/3 · Verify Pressable + WordPress MCP readiness

**What's done:** `docs/plan/phase-0-readiness.md` (commit `020a962` on `task/samblog-3`) covers everything I can verify without your dashboard — MCP adapter install paths, App Password setup, smoke-test runbook, non-MCP fallback, Substack notification-safety procedure.

**One thing to flag:** the brief mentions `docs/plan/plan.md` Phase 0 as source of truth, but Archie hasn't published that yet — I worked from the brief + acceptance criteria directly. The new doc is structured so `plan.md` can link to it when it lands.

**What I need from you (5 items, ~10 min total) — full detail in §7 of the doc:**
1. Actual WP core + PHP version on `sam.blog` (the brief says 'WP 7.0' which doesn't exist — likely a typo)
2. Greenlight to clone `sam.blog` → staging
3. Greenlight to install `mcp-adapter` plugin on staging (and `abilities-api` if WP < 6.9)
4. Create a `polly-mcp` editor user + an Application Password named `mcp-adapter-phase-0`; deliver via 1Password (NOT repo / task notes)
5. Confirm OK to repeat the smoke test on production once staging is green

**Review:** `less docs/plan/phase-0-readiness.md`

**Next step on my side:** marking samblog/3 done now since the deliverable for this task is the readiness doc itself. The live-site verification + smoke test should be a separate task once items 1–5 are answered.
