import { test, expect } from "@playwright/test";

/**
 * Keyboard scenarios.
 *
 * V0 wires only the <form> submit handler — so Enter inside the input
 * triggers send via native form submission. We verify that and probe
 * for optional shortcuts (Shift+Enter newline, j/k surface nav).
 * Probes that the UI doesn't yet implement are marked test.fixme so the
 * spec stays as a TODO trail without failing the suite.
 */

const FAKE_SURFACE = {
  session_name: "operator",
  surface_type: "operator",
  persona: "polly",
  project: "pollypm",
  window: { present: true, pane_dead: false },
};

async function stubBasics(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/chat/sessions", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ sessions: [FAKE_SURFACE] }),
    }),
  );
  await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
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
}

// Stub the dashboard read endpoint the UI fires on init so this spec
// doesn't race live daemon state under fullyParallel runs.
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

test.describe("keyboard", () => {
  test.beforeEach(async ({ page }) => {
    await stubDashboard(page);
  });

  test("Enter in input submits the send form", async ({ page }) => {
    await stubBasics(page);
    let captured: string | null = null;
    await page.route("**/api/v1/chat/operator/send", (route, req) => {
      captured = req.postData();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "{}",
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    const input = page.locator("#send-input");
    await input.fill("via enter");
    await input.press("Enter");
    await expect.poll(() => captured).not.toBeNull();
    expect(JSON.parse(captured!)).toEqual({ text: "via enter" });
  });

  test.fixme("Shift+Enter inserts newline without sending (V0 not implemented)", async ({ page }) => {
    // V0 input is <input type="text">, which can't hold newlines. This
    // would only pass once it becomes <textarea> with a keydown handler
    // that distinguishes Shift+Enter from Enter. Documented as a TODO
    // for the V1 web UI.
    await stubBasics(page);
    let hit = false;
    await page.route("**/api/v1/chat/operator/send", (route) => {
      hit = true;
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    const input = page.locator("#send-input");
    await input.focus();
    await input.type("line one");
    await page.keyboard.press("Shift+Enter");
    await input.type("line two");
    await page.waitForTimeout(250);
    expect(hit).toBe(false);
    expect(await input.inputValue()).toContain("\n");
  });

  test.fixme("j/k or arrow keys navigate surfaces (V0 not implemented)", async ({ page }) => {
    // V0 has no keyboard navigation on the surface rail. Logged as a
    // V1 web UI follow-up; this test is a placeholder so we don't lose
    // the requirement.
    await stubBasics(page);
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await list.focus();
    await page.keyboard.press("j");
    await expect(page.locator("#surface-list li.active")).toBeVisible();
  });

  test("Tab moves focus from input to send button", async ({ page }) => {
    await stubBasics(page);
    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").focus();
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => document.activeElement?.id);
    expect(focused).toBe("send-button");
  });
});
