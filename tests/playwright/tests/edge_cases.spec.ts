import { test, expect } from "@playwright/test";

/**
 * Edge cases per spec §4 (mid-tool, service unavailable, empty states).
 *
 * The UI surfaces errors via two channels:
 *   - history errors land in #message-list as `.error-banner`
 *   - the connection badge (#conn-status) flips to conn-warn / conn-error
 */

const FAKE_SURFACE = {
  session_name: "operator",
  surface_type: "operator",
  persona: "polly",
  project: "pollypm",
  window: { present: true, pane_dead: false },
};

async function stubSurfaces(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/chat/sessions", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ sessions: [FAKE_SURFACE] }),
    }),
  );
}

test.describe("edge cases", () => {
  test("409 unsafe_mid_tool surfaces in UI", async ({ page }) => {
    await stubSurfaces(page);
    await page.route("**/api/v1/chat/operator/messages*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_name: "operator",
          surface_type: "operator",
          transcript_source: "jsonl",
          messages: [],
        }),
      }),
    );
    await page.route("**/api/v1/chat/operator/send", (route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "unsafe_mid_tool", message: "agent is mid-tool-use" },
        }),
      }),
    );

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").fill("oops");
    await page.locator("#send-button").click();

    const banner = page.locator("#message-list .error-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(/mid-tool|unsafe_mid_tool/i);
  });

  test("503 service_unavailable surfaces in UI", async ({ page }) => {
    await stubSurfaces(page);
    await page.route("**/api/v1/chat/operator/messages*", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "service_unavailable", message: "daemon restarting" },
        }),
      }),
    );

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();

    const banner = page.locator("#message-list .error-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(/restart|service_unavailable|503/i);
  });

  test("empty session list shows empty-state in left rail", async ({ page }) => {
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      }),
    );
    await page.goto("/ui/");
    await expect(page.locator("#surface-list .surface-empty")).toHaveText(
      "no surfaces registered",
    );
  });

  test("sessions endpoint 500 shows error in left rail", async ({ page }) => {
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "internal", message: "boom" },
        }),
      }),
    );
    await page.goto("/ui/");
    const empty = page.locator("#surface-list .surface-empty");
    await expect(empty).toBeVisible();
    await expect(empty).toContainText(/error/i);
  });

  test("connection badge reflects HTTP failures", async ({ page }) => {
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "internal", message: "x" } }),
      }),
    );
    await page.goto("/ui/");
    await expect(page.locator("#conn-status")).toHaveClass(/conn-warn|conn-error/);
  });
});
