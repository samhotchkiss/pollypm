import { test, expect } from "@playwright/test";

/**
 * Surface list (left rail) scenarios.
 *
 * Anchors: #surface-list, .surface-empty, [data-session], #pane-title,
 * #pane-meta, #message-list, .message-empty, .audit-panel.
 */

test.describe("surfaces", () => {
  // Wait for the surface rail to reach a terminal load state: at least
  // one <li data-session> rendered, OR a `.surface-empty` element whose
  // text matches "no surfaces registered" or starts with "error:". This
  // avoids racing the async /api/v1/chat/sessions fetch (which would
  // otherwise let tests skip while data is still loading) and avoids
  // awaiting `.surface-empty.innerText()` when the element is absent
  // (which previously stalled the populated-daemon case for the full
  // actionTimeout).
  async function waitForSurfaceRailTerminal(page: import("@playwright/test").Page) {
    await page.waitForFunction(
      () => {
        const items = document.querySelectorAll(
          "#surface-list li[data-session]",
        ).length;
        const tasks = document.querySelectorAll(
          "#surface-list li[data-task]",
        ).length;
        if (items > 0 || tasks > 0) return true;
        const empty = document.querySelector(
          "#surface-list .surface-empty",
        ) as HTMLElement | null;
        if (!empty) return false;
        const text = (empty.textContent ?? "").trim();
        return (
          text.includes("no surfaces registered") || text.startsWith("error:")
        );
      },
      undefined,
      { timeout: 10_000 },
    );
  }

  async function installHealthyEventSource(page: import("@playwright/test").Page) {
    await page.addInitScript(() => {
      const instances: any[] = [];
      class MockEventSource {
        url: string;
        listeners: Record<string, EventListenerOrEventListenerObject>;
        onopen: ((event: Event) => void) | null;
        onmessage: ((event: MessageEvent) => void) | null;
        onerror: ((event: Event) => void) | null;
        closed: boolean;

        constructor(url: string) {
          this.url = url;
          this.listeners = {};
          this.onopen = null;
          this.onmessage = null;
          this.onerror = null;
          this.closed = false;
          instances.push(this);
          setTimeout(() => {
            if (!this.closed && typeof this.onopen === "function") {
              this.onopen(new Event("open"));
            }
          }, 0);
        }

        addEventListener(
          type: string,
          handler: EventListenerOrEventListenerObject,
        ) {
          this.listeners[type] = handler;
        }

        close() {
          this.closed = true;
        }
      }

      (window as any).__pollypmEventSources = instances;
      (window as any).EventSource = MockEventSource as any;
    });
  }

  function dashboardPayload(count: number) {
    return {
      rollups: { message_count_24h: count },
      daemon_status: "up",
      active_sessions: [],
      recent_messages: [],
      projects: [],
      generated_at: "2026-05-23T00:00:00Z",
      scoped_fields: [],
    };
  }

  async function stubEmptyTasks(page: import("@playwright/test").Page) {
    await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      }),
    );
  }

  async function stubEmptySessions(page: import("@playwright/test").Page) {
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      }),
    );
  }

  test("left rail renders surface list after load", async ({ page }) => {
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await expect(list).toBeVisible();
    // Either real surfaces render as <li data-session=...> or the
    // empty-state literal appears.
    await waitForSurfaceRailTerminal(page);
  });

  test("clicking a surface populates the center pane", async ({ page }) => {
    await page.goto("/ui/");
    const list = page.locator("#surface-list");
    await expect(list).toBeVisible();
    // Wait for terminal state before deciding to skip; otherwise we'd
    // race the async /chat/sessions fetch and silently no-op on live
    // daemons that do have surfaces.
    await waitForSurfaceRailTerminal(page);
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
    await waitForSurfaceRailTerminal(page);
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
    await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      }),
    );
    await page.goto("/ui/");
    await page.locator("li[data-session='fake-empty']").click();
    await expect(page.locator("#message-list .message-empty")).toHaveText(
      "no messages yet",
    );
  });

  test("SSE audit event refreshes dashboard and selected history", async ({ page }) => {
    let dashboardRequests = 0;
    let messageRequests = 0;
    let eventResponses = 0;
    let releaseEvent: (() => void) | null = null;
    const eventGate = new Promise<void>((resolve) => {
      releaseEvent = resolve;
    });

    await page.route("**/api/v1/dashboard", (route) => {
      dashboardRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          rollups: { message_count_24h: dashboardRequests },
          daemon_status: "up",
          active_sessions: [],
          recent_messages: [],
          projects: [],
          generated_at: "2026-05-23T00:00:00Z",
          scoped_fields: [],
        }),
      });
    });
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sessions: [
            {
              session_name: "operator",
              surface_type: "operator",
              persona: "polly",
              project: "pollypm",
              window: { present: true, pane_dead: false },
            },
          ],
        }),
      }),
    );
    await page.route("**/api/v1/chat/operator/messages*", (route) => {
      messageRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_name: "operator",
          surface_type: "operator",
          transcript_source: "jsonl",
          messages: [
            {
              role: "assistant",
              actor: "codex",
              ts: "2026-05-23T00:00:00Z",
              type: "message",
              text: "refresh " + messageRequests,
            },
          ],
        }),
      });
    });
    await page.route("**/api/v1/events**", async (route) => {
      await eventGate;
      eventResponses += 1;
      if (eventResponses > 1) {
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: "",
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          "event: audit\n" +
          "id: 2026-05-23T00:00:01Z\n" +
          'data: {"ts":"2026-05-23T00:00:01Z","event":"message"}\n\n',
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='operator']").click();
    await expect.poll(() => dashboardRequests).toBeGreaterThanOrEqual(1);
    await expect.poll(() => messageRequests).toBe(1);

    releaseEvent!();

    await expect.poll(() => dashboardRequests).toBeGreaterThanOrEqual(2);
    await expect.poll(() => messageRequests).toBeGreaterThanOrEqual(2);
    await expect(page.locator("#message-list .message-text")).toContainText(
      "refresh",
    );
  });

  test("dashboard refreshes coalesce while a request is in flight", async ({ page }) => {
    await installHealthyEventSource(page);
    await stubEmptySessions(page);
    await stubEmptyTasks(page);

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
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(dashboardRequests)),
      });
    });

    await page.goto("/ui/");
    await expect.poll(() => dashboardRequests).toBe(1);

    await page.evaluate(() => {
      (window as any).PollyPM.pollDashboard();
      (window as any).PollyPM.pollDashboard();
    });
    await page.waitForTimeout(250);
    expect(dashboardRequests).toBe(1);

    releaseFirst!();
    await expect.poll(() => dashboardRequests).toBe(2);
    await page.waitForTimeout(250);
    expect(dashboardRequests).toBe(2);
  });

  test("surface refreshes coalesce while a request is in flight", async ({ page }) => {
    await installHealthyEventSource(page);
    await stubEmptyTasks(page);

    let dashboardRequests = 0;
    await page.route("**/api/v1/dashboard", (route) => {
      dashboardRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(dashboardRequests)),
      });
    });

    let sessionRequests = 0;
    let releaseFirst: (() => void) | null = null;
    const firstRequestGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    await page.route("**/api/v1/chat/sessions", async (route) => {
      sessionRequests += 1;
      if (sessionRequests === 1) {
        await firstRequestGate;
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      });
    });

    await page.goto("/ui/");
    await expect.poll(() => sessionRequests).toBe(1);

    await page.evaluate(() => {
      (window as any).PollyPM.loadSurfaces();
      (window as any).PollyPM.loadSurfaces();
    });
    await page.waitForTimeout(250);
    expect(sessionRequests).toBe(1);

    releaseFirst!();
    await expect.poll(() => sessionRequests).toBe(2);
    await page.waitForTimeout(250);
    expect(sessionRequests).toBe(2);
  });

  test("initial center-pane state shows 'Select a surface'", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.locator("#pane-title")).toHaveText("Select a surface");
    await expect(page.locator("#send-input")).toBeDisabled();
    await expect(page.locator("#send-button")).toBeDisabled();
  });

  test("task surfaces render in a separate rail group", async ({ page }) => {
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
        body: JSON.stringify({
          items: [
            {
              task_id: "task-queued",
              project: "demo",
              task_number: 4,
              title: "Queued rail item",
              work_status: "queued",
              type: "task",
              priority: "normal",
              assignee: "agent-1",
              updated_at: "2026-05-23T00:00:00Z",
            },
          ],
        }),
      }),
    );
    await page.route("**/api/v1/tasks/demo/4", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          task_id: "task-queued",
          project: "demo",
          task_number: 4,
          title: "Queued rail item",
          work_status: "queued",
          type: "task",
          priority: "normal",
          assignee: "agent-1",
          updated_at: "2026-05-23T00:00:00Z",
          description: "Visible task detail",
          relationships: {},
          transitions: [],
          executions: [],
        }),
      }),
    );

    await page.goto("/ui/");
    await expect(page.locator("#surface-list .surface-group")).toHaveText([
      "Tasks",
    ]);
    await page.locator("li[data-task='demo/4']").click();
    await expect(page.locator("#pane-title")).toHaveText("demo/4");
    await expect(page.locator("#send-input")).toBeDisabled();
    await expect(page.locator("#message-list")).toContainText(
      "Visible task detail",
    );
  });

  test("audit panel expands and queries current surface scope", async ({ page }) => {
    await page.route("**/api/v1/chat/*/messages*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_name: "task-demo-4",
          surface_type: "worker",
          transcript_source: "jsonl",
          messages: [],
        }),
      }),
    );
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sessions: [
            {
              session_name: "task-demo-4",
              surface_type: "worker",
              persona: "worker",
              project: "demo",
              task_id: 4,
              window: { present: true, pane_dead: false },
            },
          ],
        }),
      }),
    );
    await page.route("**/api/v1/audit/grep**", (route) => {
      const url = new URL(route.request().url());
      expect(url.searchParams.get("project")).toBe("demo");
      expect(url.searchParams.get("pattern")).toBe("demo/4");
      expect(url.searchParams.get("limit")).toBe("25");
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          events: [
            {
              schema: 1,
              ts: "2026-05-23T00:00:00Z",
              project: "demo",
              event: "task.status_changed",
              subject: "demo/4",
              actor: "agent-1",
              status: "ok",
              metadata: {},
            },
          ],
          next_cursor: null,
        }),
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-session='task-demo-4']").click();
    await page.locator(".audit-toggle").click();
    await expect(page.locator(".audit-entry")).toContainText(
      "task.status_changed",
    );
    await expect(page.locator(".audit-entry")).toContainText(
      "2026-05-23T00:00:00Z",
    );
  });
});
