import { test, expect } from "@playwright/test";

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

async function installQuietEventSource(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    class MockEventSource {
      url: string;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        setTimeout(() => {
          if (this.onopen) this.onopen(new Event("open"));
        }, 0);
      }

      addEventListener() {}
      close() {}
    }

    (window as any).EventSource = MockEventSource as any;
  });
}

function dashboardPayload() {
  return {
    rollups: {
      open_inbox_count: 3,
      pending_plan_reviews: 1,
      alert_count: 2,
      sweep_count_24h: 5,
      message_count_24h: 8,
      tracked_count: 4,
    },
    daemon_status: "up",
    active_sessions: [{ session_name: "operator" }],
    recent_messages: [],
    projects: [],
    generated_at: "2026-05-23T00:00:00Z",
    scoped_fields: [],
  };
}

test.describe("dashboard rail", () => {
  test("rollup cards are focusable buttons that drill into visible state", async ({ page }) => {
    await installQuietEventSource(page);
    await stubSurfaces(page);
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload()),
      }),
    );

    await page.goto("/ui/");

    const expected: Array<[RegExp, string]> = [
      [/inbox/i, "operator"],
      [/plan reviews/i, "Dashboard: plan reviews"],
      [/alerts/i, "Dashboard: alerts"],
      [/activity/i, "operator"],
      [/daemon/i, "Dashboard: daemon"],
      [/active sessions/i, "operator"],
      [/projects tracked/i, "Dashboard: projects tracked"],
    ];

    for (const [name, title] of expected) {
      const card = page.getByRole("button", { name });
      await expect(card).toBeVisible();
      await expect(card).toHaveAttribute("aria-label", /Open dashboard detail/);
      await card.focus();
      await expect(card).toBeFocused();
      await page.evaluate(() => {
        const titleNode = document.getElementById("pane-title");
        const listNode = document.getElementById("message-list");
        if (titleNode) titleNode.textContent = "Reset";
        if (listNode) listNode.textContent = "Reset";
      });
      await card.click();
      await expect(page.locator("#pane-title")).toHaveText(title, {
        timeout: 750,
      });
    }
  });

  test("dashboard refresh keeps painted cards while a poll is pending", async ({ page }) => {
    await installQuietEventSource(page);
    await stubSurfaces(page);

    let dashboardRequests = 0;
    let releaseSecond: (() => void) | null = null;
    const secondRequestGate = new Promise<void>((resolve) => {
      releaseSecond = resolve;
    });

    await page.route("**/api/v1/dashboard", async (route) => {
      dashboardRequests += 1;
      if (dashboardRequests === 2) {
        await secondRequestGate;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload()),
      });
    });

    await page.goto("/ui/");
    await expect.poll(() => dashboardRequests).toBe(1);
    await expect(page.getByRole("button", { name: /inbox/i })).toBeVisible();

    await page.evaluate(() => {
      (window as any).PollyPM.pollDashboard();
    });
    await expect.poll(() => dashboardRequests).toBe(2);
    await expect(
      page.locator("#dashboard-rollups .rollup-empty.fetch-state"),
    ).toHaveCount(0, { timeout: 250 });
    await expect(page.getByRole("button", { name: /inbox/i })).toBeVisible();

    releaseSecond!();
    await expect.poll(() =>
      page.evaluate(() => (window as any).PollyPM.state.dashboardInFlight),
    ).toBe(false);
    await expect(page.getByRole("button", { name: /inbox/i })).toBeVisible();
  });

  test("slow dashboard load shows retry after three seconds", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-05-23T00:00:00Z") });
    await installQuietEventSource(page);
    await stubSurfaces(page);

    let dashboardRequests = 0;
    let releaseFirst: (() => void) | null = null;
    const firstRequestGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });

    await page.route("**/api/v1/dashboard", async (route) => {
      dashboardRequests += 1;
      if (dashboardRequests === 1) {
        await firstRequestGate;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload()),
      }).catch(() => undefined);
    });

    await page.goto("/ui/");
    await expect.poll(() => dashboardRequests).toBe(1);
    await expect(
      page.getByRole("button", { name: /Retry loading dashboard/i }),
    ).toHaveCount(0, { timeout: 100 });

    await page.clock.runFor(2500);
    await expect(
      page.getByRole("button", { name: /Retry loading dashboard/i }),
    ).toHaveCount(0, { timeout: 100 });

    await page.clock.runFor(500);
    await expect(page.locator("#dashboard-rollups .rollup-empty")).toContainText(
      /taking longer/i,
      { timeout: 750 },
    );
    await expect(
      page.getByRole("button", { name: /Retry loading dashboard/i }),
    ).toBeVisible({ timeout: 750 });

    await page.getByRole("button", { name: /Retry loading dashboard/i }).click();
    await expect.poll(() => dashboardRequests).toBe(2);

    releaseFirst!();
    await expect(page.getByRole("button", { name: /inbox/i })).toBeVisible(
      { timeout: 750 },
    );
  });
});
