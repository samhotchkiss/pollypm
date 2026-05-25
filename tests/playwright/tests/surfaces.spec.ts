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
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSED = 2;

        url: string;
        listeners: Record<string, EventListenerOrEventListenerObject[]>;
        onopen: ((event: Event) => void) | null;
        onmessage: ((event: MessageEvent) => void) | null;
        onerror: ((event: Event) => void) | null;
        readyState: number;
        closed: boolean;

        constructor(url: string) {
          this.url = url;
          this.listeners = {};
          this.onopen = null;
          this.onmessage = null;
          this.onerror = null;
          this.readyState = MockEventSource.CONNECTING;
          this.closed = false;
          instances.push(this);
          setTimeout(() => {
            if (!this.closed && typeof this.onopen === "function") {
              this.readyState = MockEventSource.OPEN;
              this.onopen(new Event("open"));
            }
          }, 0);
        }

        addEventListener(
          type: string,
          handler: EventListenerOrEventListenerObject,
        ) {
          const handlers = this.listeners[type] || [];
          handlers.push(handler);
          this.listeners[type] = handlers;
        }

        removeEventListener(
          type: string,
          handler: EventListenerOrEventListenerObject,
        ) {
          const handlers = this.listeners[type] || [];
          this.listeners[type] = handlers.filter((item) => item !== handler);
        }

        dispatch(type: string, event: Event) {
          for (const handler of this.listeners[type] || []) {
            if (typeof handler === "function") {
              handler.call(this, event);
            } else {
              handler.handleEvent(event);
            }
          }
          if (type === "message" && typeof this.onmessage === "function") {
            this.onmessage(event as MessageEvent);
          }
        }

        emit(type: string, data: string, lastEventId: string) {
          const event = new MessageEvent(type, {
            data,
            lastEventId,
          });
          this.dispatch(type, event);
        }

        fail() {
          if (this.closed) return;
          this.readyState = MockEventSource.CONNECTING;
          if (typeof this.onerror === "function") {
            this.onerror(new Event("error"));
          }
        }

        close() {
          this.closed = true;
          this.readyState = MockEventSource.CLOSED;
        }
      }

      (window as any).__pollypmEventSources = instances;
      (window as any).EventSource = MockEventSource as any;
    });
  }

  async function emitAuditEvent(
    page: import("@playwright/test").Page,
    index: number,
  ) {
    await page.evaluate((eventIndex) => {
      const source = (window as any).__pollypmEventSources[0];
      const seconds = String(eventIndex).padStart(2, "0");
      const ts = `2026-05-23T00:00:${seconds}Z`;
      source.emit(
        "audit",
        JSON.stringify({ ts, event: "message" }),
        ts,
      );
    }, index);
  }

  async function failEventSource(page: import("@playwright/test").Page) {
    await page.evaluate(() => {
      const source = (window as any).__pollypmEventSources[0];
      source.fail();
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

  async function stubEmptyProjects(page: import("@playwright/test").Page) {
    await page.route("**/api/v1/projects**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      }),
    );
  }

  async function stubEmptyActivity(page: import("@playwright/test").Page) {
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

  test("healthy SSE uses push refreshes without fallback dashboard ticks", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-05-23T00:00:00Z") });
    await installHealthyEventSource(page);
    await stubEmptySessions(page);
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

    await page.goto("/ui/");
    await page.clock.runFor(1);
    await expect.poll(() => dashboardRequests).toBe(1);

    await page.clock.runFor(5001);
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).PollyPM.state.fallbackTimer),
      )
      .toBeNull();

    await emitAuditEvent(page, 1);
    await page.clock.runFor(151);
    await expect.poll(() => dashboardRequests).toBe(2);

    await page.clock.runFor(45000);
    expect(dashboardRequests).toBe(2);

    await emitAuditEvent(page, 2);
    await page.clock.runFor(151);
    await expect.poll(() => dashboardRequests).toBe(3);

    await page.clock.runFor(30000);
    expect(dashboardRequests).toBe(3);
  });

  test("SSE error resumes dashboard fallback polling immediately", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-05-23T00:00:00Z") });
    await installHealthyEventSource(page);
    await stubEmptySessions(page);
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

    await page.goto("/ui/");
    await page.clock.runFor(1);
    await expect.poll(() => dashboardRequests).toBe(1);

    await page.clock.runFor(5001);
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).PollyPM.state.fallbackTimer),
      )
      .toBeNull();

    await failEventSource(page);
    await expect.poll(() => dashboardRequests).toBe(2);
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).PollyPM.state.fallbackTimer),
      )
      .not.toBeNull();

    await page.clock.runFor(15000);
    await expect.poll(() => dashboardRequests).toBe(3);
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
    await stubEmptyProjects(page);
    await stubEmptyActivity(page);

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

  test("initial rail load requests sessions tasks and projects in parallel", async ({ page }) => {
    await stubEmptyActivity(page);
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
      }),
    );

    let releaseSessions: (() => void) | null = null;
    const sessionsGate = new Promise<void>((resolve) => {
      releaseSessions = resolve;
    });
    let sessionRequests = 0;
    let taskRequests = 0;
    let projectRequests = 0;

    await page.route("**/api/v1/chat/sessions", async (route) => {
      sessionRequests += 1;
      await sessionsGate;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      });
    });
    await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) => {
      taskRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/v1/projects**", (route) => {
      projectRequests += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });

    await page.goto("/ui/");
    await expect.poll(() => sessionRequests).toBe(1);
    await expect.poll(() => taskRequests).toBe(1);
    await expect.poll(() => projectRequests).toBe(1);
    releaseSessions!();
    await waitForSurfaceRailTerminal(page);
  });

  test("projects render on cold load before sessions and tasks finish", async ({ page }) => {
    await installHealthyEventSource(page);
    await stubEmptyActivity(page);
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
      }),
    );

    let releaseSessions: (() => void) | null = null;
    const sessionsGate = new Promise<void>((resolve) => {
      releaseSessions = resolve;
    });
    let releaseTasks: (() => void) | null = null;
    const tasksGate = new Promise<void>((resolve) => {
      releaseTasks = resolve;
    });

    await page.route("**/api/v1/chat/sessions", async (route) => {
      await sessionsGate;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      });
    });
    await page.route(/\/api\/v1\/tasks\?limit=200$/, async (route) => {
      await tasksGate;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/v1/projects**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              key: "demo",
              name: "Demo",
              tracked: true,
              task_counts: { in_progress: 1 },
              open_inbox_count: 0,
              last_activity_at: "2026-05-23T00:00:00Z",
            },
          ],
        }),
      }),
    );

    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#project-list li[data-project='demo']")).toBeVisible();
    await expect(page.locator("#project-list")).toContainText("1 tracked");
    await expect(page.locator("#surface-list .surface-empty")).toHaveText(
      "loading surfaces...",
    );

    releaseSessions!();
    releaseTasks!();
    await waitForSurfaceRailTerminal(page);
  });

  test("rail requests time out into visible error states", async ({ page }) => {
    await installHealthyEventSource(page);
    await stubEmptyActivity(page);
    await page.addInitScript(() => {
      (window as any).__POLLYPM_RAIL_REQUEST_TIMEOUT_MS = 50;
    });
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
      }),
    );

    async function slowJson(
      route: import("@playwright/test").Route,
      body: unknown,
    ) {
      await new Promise<void>((resolve) => setTimeout(resolve, 500));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      }).catch(() => undefined);
    }

    await page.route("**/api/v1/chat/sessions", (route) =>
      slowJson(route, { sessions: [] }),
    );
    await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
      slowJson(route, { items: [] }),
    );
    await page.route("**/api/v1/projects**", (route) =>
      slowJson(route, { items: [] }),
    );

    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#project-list .project-empty")).toHaveText(
      "projects unavailable: projects request timed out after 50ms",
    );
    await expect(page.locator("#surface-list")).toContainText(
      "error: chat unavailable: chat surfaces request timed out after 50ms",
    );
    await expect(page.locator("#surface-list")).toContainText(
      "error: tasks unavailable: tasks request timed out after 50ms",
    );
  });

  test("SSE queued surface refresh preserves timeout error until success", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-05-23T00:00:00Z") });
    await installHealthyEventSource(page);
    await stubEmptyTasks(page);
    await stubEmptyProjects(page);
    await stubEmptyActivity(page);
    await page.addInitScript(() => {
      (window as any).__POLLYPM_RAIL_REQUEST_TIMEOUT_MS = 1000;
    });
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
      }),
    );

    let sessionRequests = 0;
    let secondRequestWaiting = false;
    let releaseFirst: (() => void) | null = null;
    let releaseSecond: (() => void) | null = null;
    const firstRequestGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const secondRequestGate = new Promise<void>((resolve) => {
      releaseSecond = resolve;
    });

    await page.route("**/api/v1/chat/sessions", async (route) => {
      sessionRequests += 1;
      if (sessionRequests === 1) {
        await firstRequestGate;
      } else if (sessionRequests === 2) {
        secondRequestWaiting = true;
        await secondRequestGate;
        secondRequestWaiting = false;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      }).catch(() => undefined);
    });

    await page.goto("/ui/", { waitUntil: "domcontentloaded" });
    await page.clock.runFor(1);
    await expect.poll(() => sessionRequests).toBe(1);

    await emitAuditEvent(page, 1);
    await page.clock.runFor(151);
    await expect.poll(() =>
      page.evaluate(() => (window as any).PollyPM.state.surfacesRefreshQueued),
    ).toBe(true);

    await page.clock.runFor(849);
    await expect.poll(() => sessionRequests).toBe(2);
    await expect.poll(() => secondRequestWaiting).toBe(true);
    await expect(page.locator("#surface-list")).toContainText(
      "error: chat unavailable: chat surfaces request timed out after 1s",
      { timeout: 750 },
    );

    releaseSecond!();
    await expect(page.locator("#surface-list .surface-empty")).toHaveText(
      "no surfaces registered",
      { timeout: 750 },
    );
    releaseFirst!();
  });

  test("initial center-pane state shows inline next actions", async ({ page }) => {
    await page.goto("/ui/");
    await expect(page.locator("#pane-title")).toHaveText("Ready");
    await expect(page.locator(".empty-title")).toHaveText("No surface selected");
    await expect(page.locator(".empty-action.primary")).toHaveText("Open inbox");
    await expect(page.locator("#send-input")).toBeDisabled();
    await expect(page.locator("#send-button")).toBeDisabled();
  });

  test("task surfaces render in a separate rail group", async ({ page }) => {
    await stubEmptyProjects(page);
    await stubEmptyActivity(page);
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

  test("queued task detail Start posts to claim endpoint as worker", async ({ page }) => {
    let claimActor = "";
    await stubEmptyProjects(page);
    await stubEmptyActivity(page);
    await page.route("**/api/v1/chat/sessions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      }),
    );
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
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
              assignee: "",
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
          assignee: "",
          updated_at: "2026-05-23T00:00:00Z",
          description: "Visible task detail",
          relationships: {},
          transitions: [],
          executions: [],
        }),
      }),
    );
    await page.route("**/api/v1/tasks/demo/4/claim", async (route) => {
      claimActor = (await route.request().postDataJSON()).actor;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          message: "claimed demo/4",
          warnings: [],
          task: {
            task_id: "task-queued",
            project: "demo",
            task_number: 4,
            title: "Queued rail item",
            work_status: "in_progress",
            type: "task",
            priority: "normal",
            assignee: "worker",
            updated_at: "2026-05-23T00:01:00Z",
            description: "Visible task detail",
            relationships: {},
            transitions: [],
            executions: [],
          },
        }),
      });
    });

    await page.goto("/ui/");
    await page.locator("li[data-task='demo/4']").click();
    await page.locator(".task-action-button.primary", { hasText: "Start" }).click();
    await expect.poll(() => claimActor).toBe("worker");
    await expect(page.locator("#message-list")).toContainText("in_progress");
  });

  test("project switcher filters surfaces and shows urgency", async ({ page }) => {
    await stubEmptyActivity(page);
    let dashboardProject: string | null = null;
    await page.route("**/api/v1/dashboard**", (route) => {
      const url = new URL(route.request().url());
      dashboardProject = url.searchParams.get("project");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          rollups: { tracked_count: 2, open_inbox_count: 0, pending_plan_reviews: 0 },
          daemon_status: "up",
          active_sessions: [],
          recent_messages: [],
          projects: [],
          generated_at: "2026-05-23T00:00:00Z",
          scoped_fields: [],
        }),
      });
    });
    await page.route("**/api/v1/projects**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              key: "alpha",
              name: "Alpha",
              path: "/tmp/alpha",
              tracked: true,
              kind: "git",
              glyph: "amber",
              task_counts: { blocked: 2, queued: 1 },
              open_inbox_count: 0,
              pending_plan_review: false,
              last_activity_at: "2026-05-23T00:00:00Z",
            },
            {
              key: "beta",
              name: "Beta",
              path: "/tmp/beta",
              tracked: true,
              kind: "git",
              glyph: "amber",
              task_counts: { queued: 3 },
              open_inbox_count: 0,
              pending_plan_review: false,
              last_activity_at: null,
            },
          ],
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
              session_name: "alpha-worker",
              surface_type: "worker",
              persona: "worker",
              project: "alpha",
              window: { present: true, pane_dead: false },
            },
            {
              session_name: "beta-worker",
              surface_type: "worker",
              persona: "worker",
              project: "beta",
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
    const alpha = page.locator("li[data-project='alpha']");
    await expect(alpha).toContainText("blocked");
    await expect(alpha).toContainText("2 blocked");
    await alpha.click();
    await expect(page.locator("li[data-session='alpha-worker']")).toBeVisible();
    await expect(page.locator("li[data-session='beta-worker']")).toHaveCount(0);
    await expect.poll(() => dashboardProject).toBe("alpha");
  });

  test("task rail collapses duplicate watchdog-style entries", async ({ page }) => {
    await stubEmptyProjects(page);
    await stubEmptyActivity(page);
    await stubEmptySessions(page);
    await page.route(/\/api\/v1\/tasks\?limit=200$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              task_id: "a",
              project: "demo",
              task_number: 1,
              title: "Project demo has queued tasks but no activity",
              work_status: "queued",
              type: "task",
              priority: "normal",
            },
            {
              task_id: "b",
              project: "demo",
              task_number: 2,
              title: "Project demo has queued tasks but no activity",
              work_status: "queued",
              type: "task",
              priority: "normal",
            },
            {
              task_id: "c",
              project: "demo",
              task_number: 3,
              title: "Real follow-up",
              work_status: "blocked",
              type: "task",
              priority: "high",
            },
          ],
        }),
      }),
    );

    await page.goto("/ui/");
    await expect(page.locator("li[data-task]")).toHaveCount(2);
    await expect(page.locator("li[data-task='demo/1']")).toContainText("×2");
  });

  test("activity panel renders returning-operator digest", async ({ page }) => {
    await stubEmptyProjects(page);
    await stubEmptySessions(page);
    await stubEmptyTasks(page);
    await page.route("**/api/v1/dashboard", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboardPayload(0)),
      }),
    );
    await page.route("**/api/v1/audit/stats**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          total: 3,
          by_event: { "task.status_changed": 1, "plan.approved": 1 },
          by_severity: { ok: 2, warn: 1 },
          since: "2026-05-21T00:00:00Z",
        }),
      }),
    );
    await page.route("**/api/v1/audit/grep**", (route) =>
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
              subject: "demo/7",
              actor: "agent-1",
              status: "ok",
              metadata: { to_state: "done" },
            },
            {
              schema: 1,
              ts: "2026-05-23T00:01:00Z",
              project: "demo",
              event: "watchdog.warning",
              subject: "demo",
              actor: "polly",
              status: "warn",
              metadata: {},
            },
            {
              schema: 1,
              ts: "2026-05-23T00:02:00Z",
              project: "demo",
              event: "plan.approved",
              subject: "demo/8",
              actor: "sam",
              status: "ok",
              metadata: {},
            },
          ],
          next_cursor: null,
        }),
      }),
    );

    await page.goto("/ui/");
    await expect(page.locator("#activity-summary")).toContainText("events");
    await expect(page.locator("#activity-summary")).toContainText("3");
    await expect(page.locator("#activity-summary")).toContainText("warnings");
    await expect(page.locator("#activity-summary")).toContainText("decisions");
    await expect(page.locator("#activity-feed")).toContainText("plan.approved");
    await expect(page.locator("#activity-feed")).toContainText("watchdog.warning");
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
    await page.route("**/api/v1/audit/grep**", (route) => {
      const url = new URL(route.request().url());
      if (!url.searchParams.get("pattern")) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ events: [], next_cursor: null }),
        });
      }
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
