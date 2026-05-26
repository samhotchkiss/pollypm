import { test, expect } from "@playwright/test";

/**
 * Edge cases per spec §4 (mid-tool, service unavailable, empty states).
 *
 * The UI surfaces errors via two channels:
 *   - history errors land in #message-list as `.error-banner`
 *   - send failures stay attached to the local echo as `.message-error`
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
  await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ sessions: [FAKE_SURFACE] }),
    }),
  );
}

async function stubTasks(page: import("@playwright/test").Page) {
  await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
}

// Stub the dashboard read endpoint the UI fires on init so this spec
// doesn't race live daemon state. Each test below additionally stubs
// /chat/sessions to whatever shape it needs.
async function stubDashboard(page: import("@playwright/test").Page) {
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
}

test.describe("edge cases", () => {
  test.beforeEach(async ({ page }) => {
    await stubDashboard(page);
    await stubTasks(page);
  });

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

    const inlineError = page.locator("#message-list .message-error");
    await expect(inlineError).toBeVisible();
    await expect(inlineError).toContainText(/mid-tool|unsafe_mid_tool/i);
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
    await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
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
    await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
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
    // The conn-status badge reflects the union of read-endpoint health.
    // beforeEach() above stubs /api/v1/dashboard as a successful 200, and
    // app.js fires loadSurfaces() + pollDashboard() in parallel on init.
    // If only /chat/sessions fails, the successful dashboard response can
    // win the race and flip the badge back to conn-ok. To assert the
    // failure-surfacing contract deterministically, override BOTH read
    // endpoints to fail in this specific test.
    await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "internal", message: "x" } }),
      }),
    );
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "internal", message: "x" } }),
      }),
    );
    await page.goto("/ui/");
    await expect(page.locator("#conn-status")).toHaveClass(
      /conn-warn|conn-error/,
      { timeout: 5000 },
    );
  });
});
