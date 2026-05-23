# PollyPM Behind Caddy On Tailscale

This guide covers the production-style personal deployment where Caddy
terminates HTTPS for a Tailscale `*.ts.net` name and proxies to `pm serve`.

## Recommended Same-Host Layout

Run Caddy and `pm serve` on the same machine. Bind `pm serve` to loopback so
tailnet peers can only reach PollyPM through Caddy:

```bash
pm serve --host 127.0.0.1 --port 8765
```

Use the machine's MagicDNS name as the Caddy site address:

```caddyfile
pollypm-host.tailnet-name.ts.net {
    encode zstd gzip

    reverse_proxy 127.0.0.1:8765
}
```

This keeps the cleartext backend private to the host while Caddy presents
HTTPS to tailnet clients. Because the backend request comes from loopback,
PollyPM can still issue the browser session cookie from `/ui/`.

## Tailnet-Only Backend Mode

If Caddy is on a different tailnet machine, let `pm serve` auto-detect and bind
the local Tailscale IPv4:

```bash
pm serve --port 8765
```

The startup line should say `tailscale mode` and include the detected
`100.x.y.z` address. Point Caddy at that address:

```caddyfile
pollypm-host.tailnet-name.ts.net {
    encode zstd gzip

    reverse_proxy 100.x.y.z:8765
}
```

Avoid `--host 0.0.0.0` for this deployment. An explicit `--host` override
turns PollyPM's credential-free tailnet trust off by design, and a wildcard
bind widens the listener beyond the tailnet. If you must pass `--host`, pass
the exact Tailscale IPv4 plus `--allow-remote`, then expect every request to
authenticate with a bearer token or session cookie.

## Caddy And Tailscale Certificates

Prerequisites:

- MagicDNS is enabled for the tailnet.
- HTTPS certificates are enabled in the Tailscale admin console.
- The Caddy process can ask the local `tailscaled` daemon for certificates.

Caddy's Tailscale integration can fetch a certificate for a `*.ts.net` site
from the local daemon without a `tls` block. On Debian-style packages Caddy
runs as the `caddy` user, so grant that user certificate-fetch permission:

```ini
# /etc/default/tailscaled
TS_PERMIT_CERT_UID=caddy
```

Then restart `tailscaled` and reload Caddy:

```bash
sudo systemctl restart tailscaled
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Do not commit certificate files, bearer tokens, or Caddy data directories into
the PollyPM repo. Caddy keeps its normal service state under its own data
directory, typically `/var/lib/caddy`, and Tailscale handles issuance and
renewal through `tailscaled`.

## Rotation And Reloads

With the Caddy/Tailscale integration, certificate renewal is automatic. No
PollyPM config changes or scheduled `tailscale cert` job are needed.

Reload Caddy when:

- the Caddyfile changes,
- the machine name or tailnet DNS name changes,
- `TS_PERMIT_CERT_UID` changes,
- you replaced a manual certificate-file setup with the Caddy integration.

Use:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

If you choose a manual `tailscale cert` file setup instead of the Caddy
integration, re-run `tailscale cert <machine>.<tailnet>.ts.net`, install the
new files where Caddy expects them, and reload Caddy. Prefer the integration
unless you have a concrete reason to manage files yourself.

## Sanity Checks

Check the backend:

```bash
curl -fsS http://127.0.0.1:8765/api/v1/health
```

For tailnet-only backend mode, replace `127.0.0.1` with the Tailscale IPv4.

Check the public tailnet URL:

```bash
curl -I https://pollypm-host.tailnet-name.ts.net/api/v1/health
```

Inspect service logs when TLS fails:

```bash
tailscale status
journalctl -u tailscaled -n 100 --no-pager
journalctl -u caddy -n 100 --no-pager
```

In the Tailscale admin console, the machine's certificate status should be
valid. If the browser still shows HTTP or an invalid certificate, verify the
site address in the Caddyfile exactly matches the machine's `*.ts.net` name.

## References

- Tailscale: Caddy certificates on Tailscale
  <https://tailscale.com/docs/integrations/web-servers/caddy/caddy-certificates>
- Tailscale: enabling HTTPS certificates
  <https://tailscale.com/docs/how-to/set-up-https-certificates>
- Caddy: `reverse_proxy` directive
  <https://caddyserver.com/docs/caddyfile/directives/reverse_proxy>
