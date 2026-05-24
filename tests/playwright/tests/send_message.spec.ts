import { test, expect } from "@playwright/test";

/**
 * Send-message flow.
 *
 * We mock both /sessions and /send so we never actually deliver
 * keystrokes to a real tmux pane.
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
  await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
}

async function stubMessages(page: import("@playwright/test").Page) {
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

// Stub the read endpoints the UI fires on init (dashboard rollups)
// so this spec stays a pure unit of the send-message flow and doesn't
// race live daemon state under fullyParallel runs.
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

async function stubProjects(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/projects**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
}

async function stubActivity(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/audit/stats**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total: 0,
        by_event: {},
        by_severity: {},
        since: "2026-05-21T00:00:00Z",
      }),
    }),
  );
  await page.route("**/api/v1/audit/grep**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ events: [], next_cursor: null }),
    }),
  );
}

test.describe("send message", () => {
  test.beforeEach(async ({ page }) => {
    await stubDashboard(page);
    await stubProjects(page);
    await stubActivity(page);
  });

  test("typing + clicking Send POSTs to /chat/{name}/send", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);

    let captured: { method: string; postData: string | null } | null = null;
    await page.route("**/api/v1/chat/operator/send", (route, req) => {
      captured = { method: req.method(), postData: req.postData() };
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "queued" }),
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").fill("test message from playwright");
    await page.locator("#send-button").click();

    await expect.poll(() => captured?.method).toBe("POST");
    const body = JSON.parse(captured!.postData!);
    expect(body).toEqual({ text: "test message from playwright" });
  });

  test("input clears after send", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);
    await page.route("**/api/v1/chat/operator/send", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
    );

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    const input = page.locator("#send-input");
    await input.fill("hello");
    await page.locator("#send-button").click();
    await expect(input).toHaveValue("");
  });

  test("local echo appears while send is in flight", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);

    let releaseSend: (() => void) | null = null;
    const sendGate = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await page.route("**/api/v1/chat/operator/send", async (route) => {
      await sendGate;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "queued" }),
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").fill("optimistic hello");
    await page.locator("#send-button").click();
    await expect(page.locator(".message-local-echo")).toContainText(
      "optimistic hello",
    );
    await expect(page.locator(".message-local-echo .message-type")).toHaveText(
      "sending",
    );
    releaseSend!();
    await expect(page.locator(".message-local-echo .message-type")).toHaveText(
      "pending",
    );
  });

  test("failed send leaves visible inline error", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);
    await page.route("**/api/v1/chat/operator/send", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "unsafe_mid_tool",
            message: "Refusing to send while the agent has an open tool_use",
          },
        }),
      }),
    );

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").fill("blocked send");
    await page.locator("#send-button").click();
    await expect(page.locator(".message-local-echo")).toContainText(
      "blocked send",
    );
    await expect(page.locator(".message-local-echo .message-type")).toHaveText(
      "failed",
    );
    await expect(page.locator(".message-error")).toContainText(
      "Refusing to send",
    );
    await expect(page.locator(".toast")).toContainText("send failed");
  });

  test("send is disabled until a surface is selected", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);
    await page.goto("/ui/");
    await expect(page.locator("#send-input")).toBeDisabled();
    await expect(page.locator("#stop-agent-button")).toBeHidden();
    await expect(page.locator("#send-button")).toBeDisabled();
    await page.locator("li[data-session='operator']").click();
    await expect(page.locator("#send-input")).toBeEnabled();
    await expect(page.locator("#send-button")).toBeEnabled();
  });

  test("stop-agent button interrupts mid-stream surface", async ({ page }) => {
    await stubSurfaces(page);
    await page.route("**/api/v1/chat/operator/messages*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_name: "operator",
          surface_type: "operator",
          transcript_source: "capture",
          messages: [
            {
              id: "m1",
              ts: "2026-05-23T00:00:00Z",
              role: "assistant",
              actor: "Polly",
              type: "text",
              text: "Working (0s · esc to interrupt)",
              metadata: {},
            },
          ],
        }),
      }),
    );
    let interrupted = false;
    await page.route("**/api/v1/sessions/operator/interrupt", (route, req) => {
      interrupted = req.method() === "POST";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, message: "sent Escape to operator" }),
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    const stop = page.locator("#stop-agent-button");
    await expect(stop).toBeVisible();
    await stop.click();
    await expect.poll(() => interrupted).toBe(true);
    await expect(page.locator(".toast")).toContainText("sent interrupt");
    await expect(stop).toBeHidden();
  });

  test("empty / whitespace-only text does not POST", async ({ page }) => {
    await stubSurfaces(page);
    await stubMessages(page);
    let hit = false;
    await page.route("**/api/v1/chat/operator/send", (route) => {
      hit = true;
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await page.locator("#send-input").fill("   ");
    await page.locator("#send-button").click();
    // Give the click a moment to (not) fire.
    await page.waitForTimeout(250);
    expect(hit).toBe(false);
  });
});
