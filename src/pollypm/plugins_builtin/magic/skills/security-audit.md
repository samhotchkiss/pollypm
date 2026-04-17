---
name: security-audit
description: Static analysis pass on a codebase — CodeQL/Semgrep-style rules, variant analysis, common-vuln checks.
when_to_trigger:
  - security review
  - audit for vulnerabilities
  - security scan
  - pen test prep
kind: magic_skill
attribution: https://github.com/github/codeql
---

# Security Audit

## When to use

Run before any code touches production, before a release goes public, and on a cadence (monthly for live services). The goal is to find the classes of bugs that static analysis can find — injection, authZ gaps, secret leakage, unsafe deserialization — so human review time goes to the harder classes.

## Process

1. Define the trust boundaries. What is the input (HTTP body, CLI args, file upload, queue message), where does it enter Python code, where does it cross back out (SQL, shell, filesystem, network). Draw this on a napkin before you scan.
2. Run the right scanner for the surface:
   - **Code-level**: `semgrep --config auto` for broad coverage, `bandit` for Python-specific, `CodeQL` when the org has it.
   - **Dependency CVEs**: `pip-audit`, `safety`, or GitHub's Dependabot.
   - **Secrets in history**: `trufflehog`, `gitleaks` against the full git log.
   - **Web surface**: `ffuf-web-fuzzing` skill for fuzzing; `zap-baseline` for passive scanning.
3. Triage findings by exploitability, not by severity label. A "critical" in a dev-only tool may be low risk; a "medium" in an auth path may be critical. Use the context.
4. For each real finding, do **variant analysis**: if input X is unsafe here, is input Y unsafe similarly elsewhere? One bug rarely lives alone.
5. Check the common classes explicitly:
   - SQL injection (raw string formatting into queries)
   - Command injection (`shell=True`, `os.system`)
   - Path traversal (user input in `open()`, `Path()`)
   - Deserialization (`pickle.loads` on untrusted data)
   - SSRF (user-controlled URLs fetched server-side)
   - Authorization (every endpoint checks `current_user` owns the resource)
   - Secret logging (passwords/tokens in logs)
6. Fix in priority order. File issues for findings that need design discussion; do not silently add `# nosec` to quiet the scanner.
7. Add a regression rule for each real finding. Semgrep custom rules, or a unit test that exercises the exploited path. This closes the feedback loop.

## Example invocation

```
Target: src/pollypm/ + deployed service.

Static scan:
  semgrep --config auto src/pollypm/  ->  3 findings
  bandit -r src/pollypm/               ->  2 findings (1 overlap)

Dep scan:
  pip-audit  ->  1 finding (cryptography 41.0.0 has CVE-2024-xxxx, upgrade to 42.0.3)

Secrets:
  gitleaks detect --no-git            ->  no findings

Triage:
  Finding 1 (HIGH): os.system in bin/deploy.sh called with task name
    — user-controlled via `pm task create --title "..."` -> file with shell metachars
    -> exploitable RCE on Polly host.
    Variant analysis: grep for os.system, subprocess with shell=True ->
    1 more in activity_feed/cli.py — also vulnerable.
  Finding 2 (MED): pickle.loads in memory_backends/recall.py on cached objects
    — cache file path is user-controlled via env var.
    Fix: switch to json; pickle cache was YAGNI.
  Finding 3 (LOW): logger.info includes token — scrub.

Actions:
  - Replace os.system with subprocess.run(args=[...], shell=False) — both sites.
  - Switch recall cache to JSON; delete any existing .pkl files on startup.
  - Add log-filter regex to scrub tokens.
  - Upgrade cryptography to 42.0.3.
  - Add semgrep rules: no-shell-true, no-pickle-loads, no-token-log.
  - Regression test: bin/deploy.sh fuzzed with metachars.
```

## Outputs

- A written report: target, tools run, findings, triage, actions.
- Fixes landed with regression rules in the scanner config.
- Issues filed for any finding that needs design discussion.
- Dep upgrades applied for known CVEs.

## Common failure modes

- Trusting severity labels without exploitability analysis; noise drowns signal.
- Fixing the obvious finding and skipping variant analysis; the same bug hides elsewhere.
- Silencing scanner warnings with `# nosec` instead of fixing; findings accrete.
- Skipping the regression rule; the fix may be undone in six months.
