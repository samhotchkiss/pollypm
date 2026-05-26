import { test, expect } from "@playwright/test";

/**
 * Auth scenarios.
 *
 * Auth model (per src/pollypm/web_api/auth.py and #2065 security spec):
 *   GET /ui/ mints the `pollypm-session` cookie ONLY for:
 *     1. Authorization: Bearer <token> matching the on-disk token, OR
 *     2. Loopback client (127.0.0.1, ::1), OR
 *     3. Tailscale CGNAT peer when `tailnet_trust_enabled=True`.
 *   Untrusted callers still get the HTML, but NO Set-Cookie header.
 *
 *   Subsequent /api/v1/... calls accept three credential modes:
 *     - Authorization: Bearer header
 *     - pollypm-session cookie
 *     - Tailscale CGNAT IP (when trust is enabled)
 *
 *   These tests are written for a loopback dev daemon (POLLYPM_BASE_URL
 *   defaults to http://127.0.0.1:8765), where the local cookie-mint
 *   path is exercised positively and the "no cookie + no bearer → 401"
 *   negative path is enforceable. When POLLYPM_BASE_URL points at a
 *   tailnet CGNAT address (100.x.y.z), the peer IP itself is a valid
 *   credential, so the negative tests below skip — the product is
 *   behaving correctly in that environment, just not in a way the
 *   "missing cookie" probe can falsify.
 */

const BASE_URL = process.env.POLLYPM_BASE_URL || "http://127.0.0.1:8765";
const IS_TAILNET = /\/\/100\.\d+\.\d+\.\d+/.test(BASE_URL);

test.describe("auth", () => {
  test("GET /ui/ loads and serves index.html", async ({ page }) => {
    const response = await page.goto("/ui/");
    expect(response, "navigation response").not.toBeNull();
    expect(response!.status(), "status").toBe(200);
    await expect(page).toHaveTitle(/PollyPM/);
    await expect(page.locator("#topbar .brand-name")).toHaveText("PollyPM");
  });

  test("loopback bootstrap: /ui/ mints session cookie for local caller", async ({ page, context }) => {
    // Playwright drives the browser against 127.0.0.1, so this is the
    // sanctioned local-operator path. Cookie MUST be set.
    await page.goto("/ui/");
    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === "pollypm-session");
    expect(session, "pollypm-session cookie").toBeDefined();
    expect(session!.value.length, "cookie value is non-empty").toBeGreaterThan(0);
    // Defense-in-depth: cookie must be HttpOnly so XSS can't exfiltrate
    // the bearer token.
    expect(session!.httpOnly, "cookie is HttpOnly").toBe(true);
    // SameSite=Lax keeps it off cross-site POSTs while still allowing
    // top-level navigation.
    expect(
      ["Lax", "lax"].includes(session!.sameSite ?? ""),
      "cookie SameSite=Lax",
    ).toBe(true);
  });

  test("loopback bootstrap: GET /ui/ Set-Cookie header is present", async ({ request }) => {
    // The `request` fixture in Playwright runs from the same host as
    // the browser (loopback), so we still expect a Set-Cookie. This
    // asserts the header contract directly, not just the cookie jar.
    const resp = await request.get("/ui/");
    expect(resp.status()).toBe(200);
    const setCookie = resp.headers()["set-cookie"];
    expect(setCookie, "Set-Cookie header on loopback /ui/").toBeTruthy();
    expect(setCookie).toContain("pollypm-session=");
    expect(setCookie!.toLowerCase()).toContain("httponly");
  });

  test("protected API call from JS rides the cookie", async ({ page }) => {
    await page.goto("/ui/");
    // After the page loads, app.js calls /api/v1/chat/sessions. If the
    // cookie auth works the conn-status badge becomes "online".
    const status = page.locator("#conn-status");
    await expect(status).toHaveClass(/conn-ok/, { timeout: 10_000 });
    await expect(status.locator(".conn-label")).toHaveText("online");
  });

  test("direct API call without cookie returns 401", async ({ playwright }) => {
    test.skip(
      IS_TAILNET,
      "Tailscale-trusted base URL accepts unauthenticated requests via peer IP; auth negative tests are loopback-only",
    );
    // Spin up an isolated APIRequestContext with NO cookies, NO bearer.
    // This is the canonical "untrusted client" probe — the request
    // fixture inherits state from the project, so we want a clean one.
    const ctx = await playwright.request.newContext({
      baseURL: BASE_URL,
    });
    try {
      const resp = await ctx.get("/api/v1/chat/sessions");
      // Auth middleware returns 401 specifically (not 403). The PR
      // previously accepted either; the merged auth.py always raises
      // `unauthorized()` for missing credentials, so pin to 401.
      expect(resp.status(), "status with no credentials").toBe(401);
      const body = await resp.json();
      expect(body, "error envelope shape").toHaveProperty("error");
    } finally {
      await ctx.dispose();
    }
  });

  test("cookie isolation: fresh context after /ui/ load gets NO cookie", async ({ page, playwright }) => {
    test.skip(
      IS_TAILNET,
      "Tailscale-trusted base URL accepts unauthenticated requests via peer IP; auth negative tests are loopback-only",
    );
    // Load /ui/ in the browser to mint the page's cookie.
    await page.goto("/ui/");
    const pageCookies = await page.context().cookies();
    expect(
      pageCookies.find((c) => c.name === "pollypm-session"),
      "browser context has the cookie",
    ).toBeDefined();

    // Now open a SEPARATE APIRequestContext (no shared state with the
    // browser). The cookie must NOT leak across contexts; this is the
    // invariant the previous "accept 200/401/403" test was trying to
    // express but couldn't enforce.
    const isolated = await playwright.request.newContext({
      baseURL: BASE_URL,
    });
    try {
      const resp = await isolated.get("/api/v1/chat/sessions");
      expect(resp.status(), "isolated context has no cookie → 401").toBe(401);
    } finally {
      await isolated.dispose();
    }
  });

  test("API call from page's browser context rides the cookie → 200", async ({ page }) => {
    // Positive cookie-rider invariant: after /ui/ mints the cookie,
    // requests made FROM THE SAME BROWSER CONTEXT (i.e. via the page's
    // own APIRequestContext, which shares the cookie jar) succeed.
    // This proves the JS app's `credentials: "include"` model works.
    await page.goto("/ui/");
    const resp = await page.request.get(
      "/api/v1/chat/sessions?include_transcripts=false",
    );
    expect(resp.status(), "status with cookie").toBe(200);
    const body = await resp.json();
    expect(body).toHaveProperty("sessions");
    expect(Array.isArray(body.sessions)).toBe(true);
  });
});
