---
name: ffuf-web-fuzzing
description: Authenticated web fuzzing for pentesting — auto-calibration, rate-limiting, result analysis.
when_to_trigger:
  - pentest
  - web fuzzing
  - find endpoints
  - discover routes
  - directory brute force
kind: magic_skill
attribution: https://github.com/ffuf/ffuf
---

# ffuf Web Fuzzing

## When to use

Use when you have **explicit authorization** to probe a target and need to enumerate endpoints, parameters, or vhosts. This is a pentesting skill — run it against production without permission and you are attacking a system. Use only against targets you own or have written authorization for.

## Process

1. Confirm authorization. If there is no scope document or written permission, stop.
2. Baseline the target unauthenticated: `ffuf -w /path/to/wordlist -u https://target/FUZZ -mc all -fc 404`. Watch response sizes and status codes.
3. Calibrate against false positives: `-ac` auto-calibrates by fuzzing a known-random path first and filtering anything matching that response shape. Always on for directory fuzzing.
4. Apply auth before fuzzing authenticated surface: cookies (`-b "session=..."`), headers (`-H "Authorization: Bearer ..."`), or client cert (`--client-cert`).
5. Rate limit. Default wordlists are 200k+ entries; at full speed you DoS the target and lose the signal in errors. `-rate 50` is a starting point; tune based on target response time.
6. Match thoughtfully. Start broad (`-mc all`), filter false positives (`-fc 404,403`, `-fs 1234` to drop a response of exact size), then narrow by regex on content (`-mr "admin|dashboard"`) once you see patterns.
7. Output to JSON: `-o results.json -of json`. Post-process with `jq` to filter, dedupe, and prioritize. `.results[] | select(.status == 200) | .url` pulls live URLs.
8. Follow redirects selectively. `-r` follows automatically; often you want to see the 301 to understand routing. Default: no follow, inspect redirects manually.

## Example invocation

```bash
# Auto-calibrated, rate-limited, authenticated directory fuzz
ffuf \
  -u https://target.example.com/FUZZ \
  -w /usr/share/seclists/Discovery/Web-Content/raft-large-words.txt \
  -H "Cookie: session=abc123" \
  -ac \
  -rate 50 \
  -mc all \
  -fc 404,403 \
  -o results.json -of json

# Post-process
jq '.results[] | select(.status == 200) | {url, length, status}' results.json \
  | jq -s 'sort_by(.length) | reverse' > interesting.json
```

## Outputs

- `results.json` with every matched response.
- A post-processed `interesting.json` sorted by response size (proxy for content richness).
- A written note: what was authorized, what you scanned, what you found, what you did not exploit.
- All findings logged to your scope-of-engagement tracker.

## Common failure modes

- Fuzzing without auto-calibration; you get 10,000 hits all matching the 404 template.
- Running at max rate and DoSing the target; results become noise and you risk the engagement.
- Skipping auth and missing the whole authenticated surface.
- Not recording what you did for the after-action report; clients want the exact commands you ran.
