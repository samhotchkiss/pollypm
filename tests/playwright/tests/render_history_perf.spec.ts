import { test, expect } from "@playwright/test";
import path from "node:path";

const APP_JS = path.resolve(
  __dirname,
  "../../../src/pollypm/web_api/ui/app.js",
);

type HistoryMessage = {
  id: string;
  ts: string;
  role: string;
  actor: string;
  type: string;
  text: string;
};

function messages(count: number): HistoryMessage[] {
  return Array.from({ length: count }, (_, index) => ({
    id: "msg-" + index,
    ts: "2026-05-23T00:00:" + String(index % 60).padStart(2, "0") + "Z",
    role: index % 2 === 0 ? "assistant" : "user",
    actor: index % 2 === 0 ? "codex" : "operator",
    type: "message",
    text: "message " + index,
  }));
}

async function mountUiHarness(
  page: import("@playwright/test").Page,
  history: HistoryMessage[],
) {
  await page.setContent(`
    <!doctype html>
    <html>
    <body>
      <div id="conn-status" class="conn-status">
        <span class="conn-dot"></span>
        <span class="conn-label"></span>
      </div>
      <input id="project-filter" />
      <select id="project-sort"><option value="urgency">Urgency</option></select>
      <input id="surface-filter" />
      <ul id="project-list"></ul>
      <ul id="surface-list"></ul>
      <h2 id="pane-title"></h2>
      <span id="pane-meta"></span>
      <div id="message-list"></div>
      <form id="send-form">
        <input id="send-input" />
        <button id="stop-agent-button" type="button"></button>
        <button id="send-button" type="submit"></button>
      </form>
      <select id="activity-since"><option value="2d">2d</option></select>
      <button id="activity-refresh" type="button"></button>
      <div id="dashboard-rollups"></div>
      <div id="activity-summary"></div>
      <div id="activity-feed"></div>
    </body>
    </html>
  `);

  await page.evaluate((seedHistory) => {
    (window as any).__messageRequests = 0;
    (window as any).__seedHistory = seedHistory;

    class MockEventSource {
      static OPEN = 1;

      readyState = MockEventSource.OPEN;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor() {
        setTimeout(() => {
          if (this.onopen) this.onopen(new Event("open"));
        }, 0);
      }

      addEventListener() {}
      removeEventListener() {}
      close() {}
    }

    (window as any).EventSource = MockEventSource as any;
    window.fetch = async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      let body: unknown = {};

      if (url.includes("/api/v1/chat/operator/messages")) {
        (window as any).__messageRequests += 1;
        body = {
          session_name: "operator",
          surface_type: "operator",
          transcript_source: "jsonl",
          messages: (window as any).__seedHistory,
        };
      } else if (url.includes("/api/v1/chat/sessions")) {
        body = {
          sessions: [{
            session_name: "operator",
            surface_type: "operator",
            persona: "polly",
            project: "pollypm",
            window: { present: true, pane_dead: false },
          }],
        };
      } else if (url.includes("/api/v1/tasks")) {
        body = { items: [] };
      } else if (url.includes("/api/v1/projects")) {
        body = { items: [] };
      } else if (url.includes("/api/v1/dashboard")) {
        body = {
          rollups: {},
          daemon_status: "up",
          active_sessions: [],
          recent_messages: [],
          projects: [],
          generated_at: "2026-05-23T00:00:00Z",
          scoped_fields: [],
        };
      } else if (url.includes("/api/v1/audit/stats")) {
        body = { total: 0, by_event: {}, by_severity: {}, since: null };
      } else if (url.includes("/api/v1/audit/grep")) {
        body = { events: [], next_cursor: null };
      }

      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
  }, history);

  await page.addScriptTag({ path: APP_JS });
  await page.waitForFunction(() => Boolean((window as any).PollyPM));
}

test.describe("history renderer performance", () => {
  test("caps large history payloads and skips unchanged windows", async ({ page }) => {
    await mountUiHarness(page, messages(5000));

    await page.evaluate(() => (window as any).PollyPM.selectSurface("operator"));

    await expect(page.locator("#message-list .message")).toHaveCount(50);
    await expect(page.locator("#message-list .message-text").first()).toHaveText(
      "message 49",
    );
    await expect(page.locator("#message-list .message-text").last()).toHaveText(
      "message 0",
    );

    await page.locator("#message-list .message").first().evaluate((node) => {
      node.setAttribute("data-retained", "yes");
    });
    const requestsBefore = await page.evaluate(
      () => (window as any).__messageRequests,
    );

    await page.evaluate(async () => {
      await (window as any).PollyPM.loadHistory("operator", { force: true });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__messageRequests))
      .toBe(requestsBefore + 1);
    await expect(page.locator("#message-list .message")).toHaveCount(50);
    await expect(
      page.locator("#message-list .message[data-retained='yes']"),
    ).toHaveCount(1);
  });
});
