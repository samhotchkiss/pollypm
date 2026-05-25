import { test, expect } from "@playwright/test";

const ITEM = {
  id: "demo/1",
  project: "demo",
  type: "plan_review",
  state: "waiting-on-pm",
  subject: "Plan ready",
  preview: "Review the proposed plan.",
  owner: "operator",
  thread_id: "demo/1",
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:10:00Z",
  metadata: { task_id: "demo/1", labels: ["plan_review"] },
};

const SECOND_ITEM = {
  ...ITEM,
  id: "demo/2",
  subject: "Follow-up question",
  type: "message",
  state: "open",
  updated_at: "2026-05-23T00:05:00Z",
};

async function stubChrome(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/chat/sessions", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ sessions: [] }),
    }),
  );
  await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  await page.route("**/api/v1/dashboard", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        rollups: {
          open_inbox_count: 2,
          pending_plan_reviews: 1,
          alert_count: 0,
        },
        daemon_status: "up",
        active_sessions: [],
        recent_messages: [],
        projects: [],
        generated_at: "2026-05-23T00:00:00Z",
        scoped_fields: [],
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
}

test.describe("inbox panel", () => {
  test("inbox deep link opens the inbox view on boot", async ({ page }) => {
    await stubChrome(page);
    await page.route("**/api/v1/inbox?**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [ITEM], next_cursor: null }),
      }),
    );

    await page.goto("/ui/inbox");
    await expect(page.locator("#pane-title")).toHaveText("Inbox");
    await expect(page.locator("[data-inbox-id='demo/1']")).toContainText("Plan ready");
  });

  test("dashboard inbox card opens browsable list with load more and disabled plan decisions", async ({ page }) => {
    await stubChrome(page);

    const inboxUrls: string[] = [];
    await page.route("**/api/v1/inbox?**", (route) => {
      const url = new URL(route.request().url());
      inboxUrls.push(url.search);
      const cursor = url.searchParams.get("cursor");
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          cursor === "page-2"
            ? { items: [SECOND_ITEM], next_cursor: null }
            : { items: [ITEM], next_cursor: "page-2" },
        ),
      });
    });
    await page.route("**/api/v1/inbox/demo/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...ITEM,
          messages: [
            {
              id: "demo/1#0",
              sender: "architect",
              timestamp: "2026-05-23T00:09:00Z",
              body: "Please review.",
            },
          ],
        }),
      }),
    );

    await page.goto("/ui/");
    await page.locator(".rollup-card[title='Open inbox']").click();
    await expect(page.locator("#pane-title")).toHaveText("Inbox");
    await expect(page.locator("[data-inbox-id='demo/1']")).toContainText("Plan ready");
    await expect(page.locator(".inbox-action.disabled", { hasText: "Approve" })).toBeDisabled();

    await page.locator(".inbox-load-more").click();
    await expect(page.locator("[data-inbox-id='demo/2']")).toContainText("Follow-up question");
    expect(inboxUrls.some((search) => search.includes("cursor=page-2"))).toBe(true);

    await page.locator("[data-inbox-id='demo/1']").click();
    await expect(page.locator(".inbox-thread-body")).toContainText("Please review.");
  });

  test("inbox filters and actions call existing API routes", async ({ page }) => {
    await stubChrome(page);

    let filteredRequestSeen = false;
    let markReadActor = "";
    let replyOwner = "";
    let snoozeSeconds = 0;
    let archiveReason = "";

    await page.route("**/api/v1/inbox?**", (route) => {
      const url = new URL(route.request().url());
      if (
        url.searchParams.get("project") === "demo"
        && url.searchParams.get("state") === "waiting-on-pm"
        && url.searchParams.get("type") === "plan_review"
      ) {
        filteredRequestSeen = true;
      }
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [ITEM], next_cursor: null }),
      });
    });
    await page.route("**/api/v1/inbox/demo/1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...ITEM, messages: [] }),
      }),
    );
    await page.route("**/api/v1/inbox/demo/1/mark-read", async (route) => {
      markReadActor = (await route.request().postDataJSON()).actor;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ task_id: "demo/1" }),
      });
    });
    await page.route("**/api/v1/inbox/demo/1/reply", async (route) => {
      const body = await route.request().postDataJSON();
      replyOwner = body.owner;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ task_id: "demo/1" }),
      });
    });
    await page.route("**/api/v1/inbox/demo/1/snooze", async (route) => {
      snoozeSeconds = (await route.request().postDataJSON()).duration_seconds;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ task_id: "demo/1" }),
      });
    });
    await page.route("**/api/v1/inbox/demo/1/archive", async (route) => {
      archiveReason = (await route.request().postDataJSON()).reason;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ task_id: "demo/1" }),
      });
    });

    await page.goto("/ui/");
    await page.locator(".rollup-card[title='Open inbox']").click();
    await page.locator(".inbox-filter-input").fill("demo");
    await page.locator(".inbox-filter-select").first().selectOption("waiting-on-pm");
    await page.locator(".inbox-filter-select").nth(1).selectOption("plan_review");
    await page.locator(".inbox-filter-button.primary").click();
    await expect.poll(() => filteredRequestSeen).toBe(true);

    await page.locator("[data-inbox-id='demo/1']").click();
    await page.locator(".inbox-action", { hasText: "Mark read" }).click();
    await expect.poll(() => markReadActor).toBe("operator");

    await page.locator("[data-inbox-id='demo/1']").click();
    await page.locator(".inbox-reply-input").fill("Looks good.");
    await page.locator(".inbox-reply-form .inbox-filter-button.primary").click();
    await expect.poll(() => replyOwner).toBe("operator");

    await page.locator(".inbox-action", { hasText: "Snooze 1h" }).click();
    await expect.poll(() => snoozeSeconds).toBe(3600);

    await page.locator(".inbox-action", { hasText: "Archive" }).click();
    await expect.poll(() => archiveReason).toContain("web UI");
  });

  test("plan review dashboard card opens inbox filtered to plan reviews", async ({ page }) => {
    await stubChrome(page);
    let typeFilter = "";
    await page.route("**/api/v1/inbox?**", (route) => {
      const url = new URL(route.request().url());
      typeFilter = url.searchParams.get("type") || "";
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [ITEM], next_cursor: null }),
      });
    });

    await page.goto("/ui/");
    await page.locator(".rollup-card[title='Open plan reviews']").click();
    await expect.poll(() => typeFilter).toBe("plan_review");
  });
});
