# PollyPM v0 Web UI — Playwright Tests

End-to-end tests for the v0 web UI shipped in PR #2065
(`src/pollypm/web_api/ui/`). Covers:

- cookie auth (`pollypm-session` minted by `GET /ui/` for trusted
  callers only — loopback, verified tailnet peers, or a valid bearer
  header — verified positively AND with negative cookie-isolation case)
- surface rail rendering + selection
- send-message flow against `POST /api/v1/chat/{session}/send`
- edge cases (409 unsafe_mid_tool, 503 service_unavailable, empty list)
- keyboard handling (Enter to send, plus TODO probes for the V1 UI)
- mobile viewport smoke checks (360x800) — phone-over-Tailscale is an
  advertised access mode for the v0 UI

## Auth model under test

Per `src/pollypm/web_api/auth.py` (merged in #2065), the API accepts
three credential modes:

1. `Authorization: Bearer <token>` header — any client.
2. `pollypm-session` cookie — minted by `GET /ui/` for browsers.
3. Tailscale CGNAT peer (`100.64.0.0/10`) — only when the daemon was
   built with `tailnet_trust_enabled=True` (i.e. `pm serve` bound to a
   verified Tailscale interface).

`GET /ui/` is itself gated: the `Set-Cookie` header is only emitted
for loopback callers, verified tailnet peers, or callers that present
a valid bearer token. Untrusted callers still receive the HTML but no
cookie — the SPA then surfaces a 401 on its first `/api/` call.

`tests/auth.spec.ts` exercises the loopback-bootstrap path positively
(Set-Cookie present, cookie HttpOnly, SameSite=Lax) and the negative
cookie-isolation invariant (a fresh `APIRequestContext` with no cookie
gets a hard 401, never 200/403).

## Prereqs

1. Node 18+ and npm.
2. `pm serve` (or `pm up`) running on `http://127.0.0.1:8765` with a
   valid `~/.pollypm/api-token` so the `GET /ui/` cookie endpoint can
   issue `pollypm-session`.

## Install

```bash
cd tests/playwright
npm install
npx playwright install chromium
```

## Run

In one terminal (the daemon):

```bash
pm serve --tailscale
# or just `pm up` if you already have that running
```

In another terminal (the tests):

```bash
cd tests/playwright
npx playwright test                # headless, all specs
npx playwright test auth.spec.ts   # one spec
npm run test:headed                # show the browser
npm run test:ui                    # Playwright UI mode (recommended)
npm run test:debug                 # step-through debugger
npm run report                     # open the last HTML report
```

To point the suite at a different host (e.g. a Tailscale URL):

```bash
POLLYPM_BASE_URL=http://100.x.y.z:8765 npx playwright test
```

## What gets stubbed vs. real

| Spec | Hits real daemon | Notes |
|------|------------------|-------|
| `auth.spec.ts` | yes | Verifies the real cookie handshake. |
| `surfaces.spec.ts` | partial | Real `/sessions` for live data, route stubs for the empty-state case. |
| `send_message.spec.ts` | no (route-stubbed) | We never actually send keystrokes to a real tmux pane. |
| `edge_cases.spec.ts` | no (route-stubbed) | 409/503 responses are simulated. |
| `keyboard.spec.ts` | no (route-stubbed) | |

This means the suite is safe to run while you're using PollyPM yourself:
the only requests that touch live state are read-only `GET` calls.

## Projects

- `chromium` — Desktop Chrome, default development viewport.
- `mobile-chrome` — Pixel 5 user-agent forced to a 360x800 viewport
  (typical narrow Android portrait). Run with
  `npx playwright test --project=mobile-chrome` to target it
  specifically; the default `npx playwright test` runs both.

## Known limitations / TODOs

- `Shift+Enter` newline and `j/k` surface navigation are marked
  `test.fixme` — V0 input is `<input type=text>` and the rail has no
  keyboard nav. Specs stay in place so the V1 UI work picks them up.
- No Firefox/WebKit projects yet; per the V0 PR scope we ship Chromium
  only.
- Non-loopback negative auth case (e.g. a LAN device hitting `/ui/`
  and getting NO Set-Cookie) is verified at the FastAPI layer in
  `tests/web_api/test_web_ui.py` (search for `_no_cookie_from_lan`,
  `_no_cookie_minted_from_cgnat_when_trust_disabled`,
  `_no_cookie_with_invalid_bearer`); reproducing it from Playwright
  would require spoofing TCP source IP, which isn't worth the harness
  complexity for the same invariant.

## Debugging tips

- Run `npx playwright test --ui` for the recommended workflow.
- Failures dump screenshots/videos under `playwright-report/`.
- Set `DEBUG=pw:api` to see Playwright client traffic.
- If `conn-status` won't go green, the token cookie isn't reaching the
  browser — check that `pm serve` is on 8765 and that
  `~/.pollypm/api-token` exists.
