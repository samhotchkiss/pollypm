# Web UI v0 — Security Model (ADR)

## Status

Implemented in PR #2065 on branch `feat/web-ui-v0`. This document records the
chosen security model for the v0 browser-accessible cockpit UI at `/ui/` and
its companion API at `/api/v1/*`.

## Context

The v0 web UI exposes:

- An HTML / JS SPA at `GET /ui/` served out of `src/pollypm/web_api/ui/`.
- The same `/api/v1/*` JSON surface the cockpit uses, plus an SSE stream at
  `/events`.

The auth boundary had to defend against:

- **LAN exposure** — a non-Tailscale device on the same Wi-Fi reaching the
  port and either viewing UI state or driving the API.
- **CGNAT spoofing** — RFC 6598 shared address space (`100.64.0.0/10`) is
  used by Tailscale but also by some ISPs' CGNAT infrastructure. A client
  whose `client.host` happens to land in that range must not be trusted as a
  tailnet peer unless the server actually bound to the tailnet interface.
- **Credential issuance from a public route** — the SPA needs a session
  cookie to make API calls, but `GET /ui/` cannot mint that cookie for any
  caller who can reach the route, or any LAN device walks into a fully
  authenticated session.
- **Public exposure footguns** — operators who explicitly choose
  `--host 0.0.0.0 --allow-remote` (e.g. behind their own TLS reverse proxy)
  must not silently inherit Tailscale's CGNAT trust.

## Decision

### Listener binding — `pm serve`

Implemented in `src/pollypm/cli_features/web_api.py` (the `serve_command`
body, roughly lines 244–290).

- **Default (no flags):** call `detect_tailscale_ip()`, which shells out to
  `tailscale ip -4`, parses the first non-empty token through
  `ipaddress.IPv4Address`, AND checks membership in
  `_TAILSCALE_CGNAT_NET` (`100.64.0.0/10`) before returning. If that returns
  a verified tailnet IPv4, bind that interface only (`bind_mode="tailscale"`,
  `tailnet_trust=True`). Otherwise fall back to `127.0.0.1`
  (`bind_mode="loopback"`, `tailnet_trust=False`). LAN access is intentionally
  unsupported in v0.
- **`--host <addr>` explicit override:** honored verbatim, skipping
  detection. A non-loopback host still requires `--allow-remote` (spec §3).
  `tailnet_trust` stays `False` *unless* the explicit host string is exactly
  equal to the value `detect_tailscale_ip()` would have returned — i.e. the
  operator manually typed the same tailnet IPv4 the detector verified. Any
  other explicit host (loopback, `0.0.0.0`, a LAN address) leaves
  `tailnet_trust=False` so CGNAT-source peers do NOT get credential-free
  access on an unverified bind.
- **`--tailscale` flag:** deprecated no-op kept for back-compat. The
  auto-detection it used to gate is now unconditional.

### Cookie issuance — `GET /ui/`

Implemented in `src/pollypm/web_api/app.py` at the `_ui_index` handler
(around line 350+).

The `pollypm-session` cookie is minted only when at least one of the
following holds:

- **Loopback caller** — `request.client.host` is `127.0.0.1` or `::1`. A
  browser on the same machine as the daemon is treated as the local
  operator. This is a deliberate convenience bypass; see the docstring on
  `src/pollypm/web_api/auth.py` for the trade-off note.
- **Verified tailnet peer** — `request.client.host` is in
  `_TAILSCALE_CGNAT_NET` *and* the app was constructed with
  `tailnet_trust_enabled=True` (i.e. the serve path actually bound to a
  verified Tailscale IPv4).
- **Valid bearer header** — the caller already presented
  `Authorization: Bearer <token>` matching the on-disk token, so issuing the
  cookie is just a UX convenience for subsequent requests.

Any other caller (e.g. a non-Tailscale LAN device on a server that happens
to bind wider, or a CGNAT-source peer on an unverified bind) receives the
HTML without a `Set-Cookie` header. The SPA will then receive 401 on its
first API call.

### API auth — `make_bearer_auth_dependency`

Implemented in `src/pollypm/web_api/auth.py`. Three credential modes, in
order:

1. `Authorization: Bearer <token>` header — always accepted, constant-time
   comparison against the on-disk token (`~/.pollypm/api-token`, mode 0600).
   Token rotation via `pm api regen-token` invalidates outstanding sessions
   on the next request because the dependency reads fresh from disk.
2. `pollypm-session` cookie — same comparison rules as the header. A stale
   cookie (from a pre-rotation session) returns a friendlier
   `invalid_token` message hinting at a `/ui/` reload.
3. Credential-free Tailscale CGNAT trust — only when the app was built
   with `tailnet_trust_enabled=True`. Without that gate, a CGNAT-source
   peer on a public bind would walk past auth, so the flag is the only
   thing tying this mode to a verified tailnet bind.

The SSE dependency (`make_sse_auth_dependency`) additionally accepts
`?token=` as a query-string fallback because the browser `EventSource` API
cannot send custom headers. The query-string mode is SSE-only — every other
endpoint rejects it — so bearer tokens don't end up in proxy access logs
for normal traffic.

## Trade-offs

- **LAN devices without Tailscale cannot use the UI** by default. This is
  documented as a Tailscale-first deployment in the `pm serve` help text and
  in `docs/web-api-spec.md`. Operators who genuinely want LAN access can
  pass `--host <lan-ip> --allow-remote` and paste the bearer manually, but
  they explicitly opt out of CGNAT trust by doing so.
- **Loopback cookie issuance is a deliberate convenience bypass.** A local
  process without the bearer token still gets 401 from the API itself —
  only the `/ui/` HTML route mints the cookie for loopback. The trade-off
  is documented inline in `src/pollypm/web_api/auth.py`'s module docstring
  (the "loopback-cookie-mint is an explicit local-operator bypass, not an
  oversight" note).
- **CGNAT range as defense-in-depth, not the trust boundary.** The
  primary tailnet enforcement is the OS-level interface bind done by
  `pm serve`; the in-process `is_tailscale_ip()` check exists so a future
  reverse-proxy deployment that loses the interface binding still drops
  random LAN devices.

## References

- `src/pollypm/web_api/auth.py` — bearer + cookie + tailnet dependencies,
  with the full trust-boundary docstring on the module and on
  `make_bearer_auth_dependency`.
- `src/pollypm/web_api/app.py` — cookie issuance gate in `_ui_index`.
- `src/pollypm/cli_features/web_api.py` — `detect_tailscale_ip()` and the
  bind-mode selection in `serve_command`.
- `tests/web_api/test_web_ui.py` — security regressions covering the
  cookie gate, detector validation, and `tailnet_trust` propagation.
- `tests/web_api/test_auth.py` — bearer / cookie / CGNAT auth-dependency
  unit tests.
- `docs/web-api-spec.md` §3 — operator-facing spec for the bind / auth
  surface.

## Implemented in

PR #2065 — landed [DATE TBD].
