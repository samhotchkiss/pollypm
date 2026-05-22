import { test, expect } from "@playwright/test";

/**
 * Surface list (left rail) scenarios.
 *
 * Anchors: #surface-list, .surface-empty, [data-session], #pane-title,
 * #pane-meta, #message-list, .message-empty.
 */

test.describe("surfaces", () => {
  test("left rail renders surface list after load", async ({ page }) => {
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await expect(list).toBeVisible();
    // Either real surfaces render as <li data-session=...> or the
    // empty-state literal appears.
    await expect
      .poll(async () => {
        const items = await list.locator("li[data-session]").count();
        const emptyText = await list.locator(".surface-empty").innerText().catch(() => "");
        return items > 0 || emptyText.includes("no surfaces registered") || emptyText.startsWith("error:");
      }, { timeout: 10_000 })
      .toBe(true);
  });

  test("clicking a surface populates the center pane", async ({ page }) => {
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await expect(list).toBeVisible();
    const items = list.locator("li[data-session]");
    const count = await items.count();
    test.skip(count === 0, "no surfaces registered on this daemon");
    const first = items.first();
    const sessionName = await first.getAttribute("data-session");
    await first.click();
    await expect(page.locator("#pane-title")).toHaveText(sessionName!);
    await expect(page.locator("#send-input")).toBeEnabled();
    await expect(page.locator("#send-button")).toBeEnabled();
  });

  test("each surface type can be selected if present", async ({ page }) => {
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await expect(list).toBeVisible();
    const items = await list.locator("li[data-session]").all();
    test.skip(items.length === 0, "no surfaces registered on this daemon");
    // Click up to 4 surfaces and assert pane swaps each time.
    for (const item of items.slice(0, 4)) {
      const name = await item.getAttribute("data-session");
      await item.click();
      await expect(page.locator("#pane-title")).toHaveText(name!);
      // Message list either shows messages or "no messages yet".
      const ml = page.locator("#message-list");
      await expect(ml).toBeVisible();
    }
  });

  test("empty session shows 'no messages yet' placeholder", async ({ page }) => {
    // Stub the messages endpoint to return an empty list.
    await page.route("**/api/v1/chat/*/messages*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_name: "fake-empty",
          surface_type: "worker",
          transcript_source: "jsonl",
          messages: [],
        }),
      }),
    );
    // Stub the sessions endpoint to ensure at least one selectable surface.
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sessions: [
            {
              session_name: "fake-empty",
              surface_type: "worker",
              persona: "worker",
              project: "demo",
              window: { present: true, pane_dead: false },
            },
          ],
        }),
      }),
    );
    await page.goto("/ui/");
    await page.locator("li[data-session='fake-empty']").click();
    await expect(page.locator("#message-list .message-empty")).toHaveText(
      "no messages yet",
    );
  });

  test("initial center-pane state shows 'Select a surface'", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.locator("#pane-title")).toHaveText("Select a surface");
    await expect(page.locator("#send-input")).toBeDisabled();
    await expect(page.locator("#send-button")).toBeDisabled();
  });
});
