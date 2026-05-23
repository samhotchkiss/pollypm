# PR #2065 Web UI — Security Fix Spec

**Status:** Awaiting Sam's sign-off before implementation dispatch.
**Date:** 2026-05-22
**Scope:** Address the 4 Codex P0s on PR #2065 without re-litigating the v0 UI design.

The goal of this spec is to get an answer from you on three design choices, then dispatch one fix agent with a clear contract. No autonomous security implementation.

---

## TL;DR — the three decisions you need to make

1. **Cookie issuance model** (P0 #1): how does `/ui/` decide whether the caller is a trusted local operator before handing them the bearer-as-cookie?
2. **Tailscale interface binding** (P0 #3): should `--tailscale` bind ONLY to the tailnet interface address (losing loopback unless you re-add it), or bind both?
3. **Stack ordering** (P0 #4): merge #2057 first then rebase #2065, vs cherry-pick the dashboard route onto the #2065 branch.

Recommendations below; the rest of this doc explains the bugs and fix options.

---

## The 4 P0s, restated from code

### P0 #1 — `/ui/` mints a cookie for any caller, unauthenticated

`src/pollypm/web_api/app.py` lines 250–278 (on `feat/web-ui-v0`):

```python
@app.get("/ui/", include_in_schema=False)
def _ui_index() -> Response:
    resolved_token_path = token_path or DEFAULT_TOKEN_PATH
    token_value = load_token(resolved_token_path)
    response = FileResponse(index_path, media_type="text/html")
    if token_value:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token_value,
            httponly=True,
            samesite="lax",
            secure=False,
            ...
        )
    return response
```

There is NO auth check before `set_cookie`. Anyone who can reach `/ui/` over the network — LAN device, spoofed CGNAT source IP, anyone on a misconfigured deploy — receives the bearer token in a `Set-Cookie` header and is thereafter fully authenticated for every API call.

This is credential issuance from a public route.

### P0 #2 — `100.64.0.0/10` (CGNAT) trust without verifying tailnet membership

`src/pollypm/web_api/auth.py` line 47 + `is_tailscale_ip()`:

```python
_TAILSCALE_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

def is_tailscale_ip(host: str | None) -> bool:
    addr = ipaddress.ip_address(host)
    return addr in _TAILSCALE_CGNAT_NET
```

Used in `make_bearer_auth_dependency` to grant unauthenticated access to any caller whose `request.client.host` falls in the CGNAT range.

The CGNAT range is RFC 6598 shared address space. Tailscale is one user of it but not the only one. A non-Tailscale device on a LAN that uses 100.64/10 internally, or a packet crafted with a spoofed source IP if the kernel allows it through, would be trusted as a tailnet peer.

CGNAT range membership is necessary but not sufficient proof of tailnet membership.

### P0 #3 — `--tailscale` binds 0.0.0.0

`src/pollypm/cli_features/web_api.py` lines 200–211:

```python
if tailscale:
    tailscale_ip = detect_tailscale_ip()
    ...
    else:
        host = "0.0.0.0"
        allow_remote = True
```

The intent of `--tailscale` is "expose this only to my tailnet." The implementation binds every interface, then leans on the CGNAT auth check (P0 #2) to gate. With CGNAT trust broken, every interface is a real attack surface.

### P0 #4 — UI calls `/api/v1/dashboard` not on the branch base

`src/pollypm/web_api/ui/app.js` line ~209 calls `GET /api/v1/dashboard`. That route is not on `origin/main` — it lives in PR #2057. Stacked-PR hazard: if #2065 merges before #2057, the UI 404s on every right-rail poll.

---

## Fix proposals

### Fix #1 (cookie issuance) — proposed: gate on auth path BEFORE cookie set

The cookie is convenience credential; the bearer is the master credential. The boot path that issues the cookie should require ONE of these signals first:

- **(a) Authorization header present and valid** → set the cookie, return the page.
- **(b) Client IP is loopback (127.0.0.1, ::1)** → set the cookie, return the page. Local operator on the Mac itself.
- **(c) Client IP is verified Tailscale peer** (per fix #2 + #3 below) → set the cookie, return the page. Operator hitting `/ui/` from their phone over Tailscale.
- **(d) None of the above** → serve the HTML, do NOT set the cookie. SPA will hit `/api/v1/...` and get 401, and surfaces "paste your bearer token to bootstrap" UX.

Implementation note: `app.js` already needs a "no cookie yet" fallback — the SPA currently assumes the cookie is always set. We add a small bootstrap form that, on 401, prompts for the bearer token and POSTs it to a new `POST /ui/bootstrap` endpoint that sets the cookie after validating. (Or: skip the bootstrap UI in v0 and document "ssh to the Mac, hit /ui/ once, copy the cookie" — uglier but smaller diff.)

**Decision needed from you:** which fallback UX for path (d)?
- (d-i) Build a small `<input>` paste box in the SPA that exchanges bearer for cookie via `POST /ui/bootstrap`. Most polished. ~50 LOC delta.
- (d-ii) Document "first cookie must be obtained from a loopback or Tailscale visit; LAN devices not supported in v0." Smallest diff; matches Tailscale-only deployment intent.

Recommendation: **d-ii**. Personal-use, Tailscale-first deployment, no need to support LAN at all. If you ever expose this on the public internet, you'll be revisiting this anyway.

### Fix #2 (CGNAT verification) — proposed: bind-to-interface enforces tailnet at OS layer

Instead of comparing `request.client.host` against a CGNAT range string (which a spoofed source IP defeats), bind uvicorn to the detected Tailscale IP only. The Linux/Darwin routing table then enforces that ONLY packets arriving on `tailscale0` (or its peers) can reach the listening socket. Source-IP spoof from another interface is dropped before the application sees it.

Pair with: keep `is_tailscale_ip()` as a defense-in-depth check (in case a misconfigured deploy binds wider), but it's no longer load-bearing.

Optional stronger version: also query `tailscale status --json` to fetch peer IPs and assert `request.client.host` matches a known peer. Higher friction (requires tailscale CLI in PATH, adds latency to every auth check). Skip unless you want belt-and-suspenders.

Recommendation: **bind-to-interface only**, keep the CGNAT check as a sanity filter, skip peer enumeration. Simpler, OS-enforced, no new runtime dependency.

### Fix #3 (`--tailscale` bind) — proposed: bind to Tailscale IP, not 0.0.0.0

`src/pollypm/cli_features/web_api.py:208`: change

```python
host = "0.0.0.0"
```

to

```python
host = tailscale_ip   # e.g. "100.x.y.z"
```

Implications:
- Loopback access (`http://127.0.0.1:8765/ui/`) NO LONGER WORKS when started with `--tailscale`. The operator accesses via `http://<tailscale-ip>:8765/ui/` from any tailnet device including the Mac itself.
- The OS routing table guarantees only packets arriving on `tailscale0` reach the socket.
- Combined with Fix #2, `is_tailscale_ip()` becomes a sanity check on top of OS enforcement.

**Decision needed from you:** is losing loopback-while-tailscale acceptable, or do you want both?

- (a) **Tailscale IP only.** Loopback unavailable when `--tailscale`. Smallest, most defensible. Operator hits `http://<tailscale-ip>:8765/ui/` even from the Mac. Recommended.
- (b) **Two binds.** Spin up uvicorn with `host="0.0.0.0"` BUT add an explicit IP-allow-list middleware that drops connections whose accepted-on interface isn't loopback or tailscale0. Possible via `socket.getsockname()` on the accepted socket. More complex; uvicorn doesn't expose this directly so might need a custom Server class.
- (c) **Two uvicorn processes.** Bind one to `127.0.0.1`, one to `<tailscale-ip>`. Separate processes, double the memory, but trivial config. Acceptable if (a) is too inconvenient.

Recommendation: **(a)**. You will likely hit `/ui/` from your phone more than from the Mac, and when you're on the Mac you can use the tailnet IP just as easily.

### Fix #4 (stack dependency) — proposed: merge #2057 first, then rebase #2065

The fix agent I dispatched for #2057 will push corrections to the existing PR. Once #2057 merges, I rebase #2065 on top of new `main`. The dashboard route is then live and `app.js`'s `GET /api/v1/dashboard` works.

Alternative: cherry-pick #2057's commits into #2065 to break the dep. Don't do this — it creates merge conflicts when #2057 lands.

Recommendation: **merge order #2057 → #2065**, with rebase between.

---

## Proposed implementation contract for the fix agent

Once you sign off the three decisions above, the agent gets a tight scope:

1. **`/ui/` cookie gating** — implement the d-ii fallback (cookie set only on loopback OR verified-Tailscale OR valid-bearer-header). Tests: assert no cookie set when client IP is `192.168.1.x`; assert cookie set when loopback; assert cookie set when valid Authorization header.
2. **`--tailscale` interface binding** — change `host="0.0.0.0"` to `host=tailscale_ip`. Test: simulate `detect_tailscale_ip()` returning `100.64.0.5`, assert uvicorn is invoked with that exact host.
3. **CGNAT defense-in-depth check** — keep `is_tailscale_ip()` but document in the docstring that it's a sanity filter on top of interface binding, not the primary trust boundary.
4. **Mobile/tablet CSS** (the P1) — add a 360px media query and Playwright mobile viewport screenshot.
5. **Rebase after #2057** — once #2057 merges, rebase #2065 onto new main. Run the test suite. Confirm `/api/v1/dashboard` resolves.

Reply to all 5 Codex inline threads on PR #2065 with file:line of each fix.

---

## Acceptance criteria

After fixes land:
- `curl -sS -i http://<lan-ip>:8765/ui/` from a non-Tailscale LAN device returns the HTML BUT no `Set-Cookie` header. Subsequent `/api/v1/*` calls without bearer return 401.
- `curl -sS -i http://<tailscale-ip>:8765/ui/` from another tailnet device returns the HTML AND `Set-Cookie: pollypm-session=...`. Subsequent calls work.
- `lsof -nP -i :8765` while running `pm serve --tailscale` shows ONE listener bound to the Tailscale IP, NOT `*:8765`.
- All 14 tests in `tests/web_api/test_web_ui.py` pass; new negative-auth tests added.

---

## What you need to do next

Reply with:
1. Cookie fallback: **d-i** (paste-box bootstrap) or **d-ii** (Tailscale-only, document it)?
2. Bind mode: **(a)** Tailscale IP only, **(b)** allow-list middleware, or **(c)** two uvicorn processes?
3. Confirm stack order: merge #2057 first then rebase #2065. (Default yes; flag if you want me to do something else.)

When you answer, I dispatch the fix agent with the exact contract above.
