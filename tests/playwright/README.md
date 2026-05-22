# PollyPM v0 Web UI — Playwright Tests

End-to-end tests for the v0 web UI shipped in PR #2065
(`src/pollypm/web_api/ui/`). Covers:

- cookie auth (`pollypm-session` set by `GET /ui/`)
- surface rail rendering + selection
- send-message flow against `POST /api/v1/chat/{session}/send`
- edge cases (409 unsafe_mid_tool, 503 service_unavailable, empty list)
- keyboard handling (Enter to send, plus TODO probes for the V1 UI)

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

## Known limitations / TODOs

- `Shift+Enter` newline and `j/k` surface navigation are marked
  `test.fixme` — V0 input is `<input type=text>` and the rail has no
  keyboard nav. Specs stay in place so the V1 UI work picks them up.
- No mobile viewport project yet; V0 layout is desktop-only.
- No Firefox/WebKit projects yet; per the V0 PR scope we ship Chromium
  only.

## Debugging tips

- Run `npx playwright test --ui` for the recommended workflow.
- Failures dump screenshots/videos under `playwright-report/`.
- Set `DEBUG=pw:api` to see Playwright client traffic.
- If `conn-status` won't go green, the token cookie isn't reaching the
  browser — check that `pm serve` is on 8765 and that
  `~/.pollypm/api-token` exists.
