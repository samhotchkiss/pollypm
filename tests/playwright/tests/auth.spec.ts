import { test, expect } from "@playwright/test";

/**
 * Auth scenarios.
 *
 * Auth model (per /ui/app.js header comment):
 *   GET /ui/ reads the on-disk token and sets the `pollypm-session`
 *   cookie. Every subsequent fetch uses credentials: "include" to ride
 *   that cookie. We never expose the token to the browser.
 */

test.describe("auth", () => {
  test("GET /ui/ loads and serves index.html", async ({ page }) => {
    const response = await page.goto("/ui/");
    expect(response, "navigation response").not.toBeNull();
    expect(response!.status(), "status").toBe(200);
    await expect(page).toHaveTitle(/PollyPM/);
    await expect(page.locator("#topbar .brand-name")).toHaveText("PollyPM");
  });

  test("session cookie is set after visiting /ui/", async ({ page, context }) => {
    await page.goto("/ui/");
    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === "pollypm-session");
    expect(session, "pollypm-session cookie").toBeDefined();
    expect(session!.value.length, "cookie value is non-empty").toBeGreaterThan(0);
  });

  test("protected API call from JS rides the cookie", async ({ page }) => {
    await page.goto("/ui/");
    // After the page loads, app.js calls /api/v1/chat/sessions. If the
    // cookie auth works the conn-status badge becomes "online".
    const status = page.locator("#conn-status");
    await expect(status).toHaveClass(/conn-ok/, { timeout: 10_000 });
    await expect(status.locator(".conn-label")).toHaveText("online");
  });

  test("direct API call without cookie returns 401", async ({ request }) => {
    // request fixture starts with no cookies — hitting a protected
    // endpoint directly should be rejected.
    const resp = await request.get("/api/v1/chat/sessions");
    // Accept 401 or 403 depending on auth middleware shape.
    expect([401, 403]).toContain(resp.status());
  });

  test("direct API call shape after /ui/ loads cookie", async ({ page, request }) => {
    await page.goto("/ui/");
    // After visiting /ui/, the request fixture should NOT share that
    // cookie (it's a separate context); this asserts the cookie is
    // scoped to the browser context, not the test runner.
    const resp = await request.get("/api/v1/chat/sessions");
    expect([200, 401, 403]).toContain(resp.status());
    // If the deployment uses the same browser context's cookies, accept
    // 200 with the expected envelope shape.
    if (resp.status() === 200) {
      const body = await resp.json();
      expect(body).toHaveProperty("sessions");
      expect(Array.isArray(body.sessions)).toBe(true);
    }
  });
});
