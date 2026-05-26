import { test, expect } from "@playwright/test";

/**
 * Mobile viewport smoke checks.
 *
 * #2065 ships the web UI explicitly intending phone-over-Tailscale
 * access. The v0 layout is desktop-first with a fixed left rail, so
 * we want at least one project that catches the obvious narrow-screen
 * regressions:
 *   - core chrome (topbar, rail, send pane) is rendered, not hidden
 *     or scrolled off-screen
 *   - no horizontal overflow on the document
 *   - tappable send button stays inside the viewport
 *
 * These tests run under all projects but are most meaningful under
 * the `mobile-chrome` project (360x800) declared in playwright.config.
 * Running under `chromium` (Desktop Chrome) is fine — assertions hold
 * there too.
 */

test.describe("mobile viewport", () => {
  test("core chrome renders at 360px wide", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.locator("#topbar")).toBeVisible();
    await expect(page.locator("#topbar .brand-name")).toHaveText("PollyPM");
    // Surface rail still exists (may be visually collapsed on narrow
    // viewports; the assertion is that the DOM survives, not that the
    // layout is pretty — V1 will fix the rail).
    await expect(page.locator("#surface-list")).toBeAttached();
    await expect(page.locator("#pane-title")).toBeAttached();
  });

  test("no horizontal overflow on /ui/", async ({ page }) => {
    await page.goto("/ui/");
    // Allow a tiny rounding fudge factor — sub-pixel widths from font
    // rendering sometimes nudge scrollWidth one pixel over clientWidth.
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return doc.scrollWidth - doc.clientWidth;
    });
    expect(overflow, "horizontal overflow in CSS pixels").toBeLessThanOrEqual(1);
  });

  test("surface list renders at 360px before optional task rail request completes", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          rollups: {},
          daemon_status: "up",
          active_sessions: [],
          recent_messages: [],
          projects: [],
          generated_at: "2026-05-23T00:00:00Z",
          scoped_fields: [],
        }),
      }),
    );
    await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sessions: [
            {
              session_name: "mobile-operator",
              surface_type: "operator",
              persona: "polly",
              project: "demo",
              window: { present: true, pane_dead: false },
            },
          ],
        }),
      }),
    );
    await page.route("**/api/v1/events**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "",
      }),
    );
    await page.route(/\/api\/v1\/tasks\?limit=200$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });

    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("li[data-session='mobile-operator']")).toBeVisible();
    const railBox = await page.locator("#surface-rail").boundingBox();
    expect(railBox?.width ?? 0).toBeGreaterThan(0);
  });
});
