import { test, expect } from "@playwright/test";

async function stubChrome(page: import("@playwright/test").Page) {
  await page.route(/\/api\/v1\/chat\/sessions(\?.*)?$/, (route) =>
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
  await page.route("**/api/v1/projects**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  await page.route("**/api/v1/dashboard**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        rollups: { open_inbox_count: 0, pending_plan_reviews: 0, alert_count: 1 },
        daemon_status: "up",
        active_sessions: [],
        recent_messages: [],
        projects: [],
        generated_at: "2026-05-23T00:00:00Z",
        scoped_fields: [],
      }),
    }),
  );
  await page.route("**/api/v1/doctor/report", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ checks: [] }),
    }),
  );
  await page.route("**/api/v1/audit/stats**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: 0, by_event: {}, by_severity: {} }),
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

test.describe("alerts panel", () => {
  test("acknowledge action posts and refreshes the alert list", async ({ page }) => {
    await stubChrome(page);

    let acknowledged = false;
    await page.route(/\/api\/v1\/alerts\?limit=100$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          generated_at: "2026-05-23T00:00:00Z",
          total: acknowledged ? 0 : 1,
          alerts: acknowledged
            ? []
            : [{
                id: 42,
                session_name: "worker_demo",
                alert_type: "auth_broken",
                severity: "error",
                message: "Codex auth failed",
                status: "open",
                channel: "action_required",
                created_at: "2026-05-23T00:00:00Z",
                updated_at: "2026-05-23T00:01:00Z",
                actions: [{
                  kind: "acknowledge",
                  label: "Acknowledge",
                  hint: "Clear this alert",
                }],
              }],
        }),
      }),
    );
    await page.route(/\/api\/v1\/alerts\/42\/actions\/acknowledge$/, (route) => {
      acknowledged = true;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          message: "acknowledged alert #42",
          alert_id: 42,
          status: "closed",
        }),
      });
    });

    await page.goto("/ui/alerts");
    await expect(page.getByRole("button", { name: "Acknowledge" })).toBeVisible();
    await page.getByRole("button", { name: "Acknowledge" }).click();
    await expect.poll(() => acknowledged).toBe(true);
    await expect(
      page.locator(".alerts-panel .activity-empty", {
        hasText: "no action-required alerts",
      }),
    ).toBeVisible();
  });
});
