/*
 * Wave-1.5 §03 deeper probe against LIVE pm serve at 100.67.2.108:8765.
 * Measures: cold paint, project click, surface click, multi-tab switch,
 * inbox open/dismiss, error envelope rendering, SSE refresh behavior,
 * unknown-surface failure handling.
 *
 * Output: JSON to /tmp/wave15-results.json plus stdout summary.
 */
const { chromium } = require('@playwright/test');
const fs = require('fs');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

const out = {
  ts: new Date().toISOString(),
  base: BASE,
  scenarios: {},
  click_latencies_ms: [],
  console_errors: [],
  network_errors: [],
};

function record(scenario, data) {
  out.scenarios[scenario] = data;
  console.log(`\n=== ${scenario} ===`);
  console.log(JSON.stringify(data, null, 2));
}

async function newPage(browser, { width = 1280, height = 900 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  const p = await ctx.newPage();
  p.on('console', msg => {
    if (msg.type() === 'error') out.console_errors.push({ ts: Date.now(), text: msg.text() });
  });
  p.on('pageerror', err => out.console_errors.push({ ts: Date.now(), text: 'pageerror:' + err.message }));
  p.on('requestfailed', req => out.network_errors.push({ url: req.url(), failure: req.failure() }));
  return { ctx, page: p };
}

async function run() {
  const browser = await chromium.launch({ headless: true });

  // ========== SCENARIO 1: Dashboard cold-load ==========
  {
    const { ctx, page } = await newPage(browser);
    const t0 = Date.now();
    const navResp = await page.goto(UI, { waitUntil: 'domcontentloaded' });
    const tDom = Date.now() - t0;

    // Wait for project list to populate (post-#2283)
    let projectsRenderedMs = null;
    try {
      await page.waitForFunction(() => {
        const ul = document.querySelector('#project-list');
        if (!ul) return false;
        const lis = ul.querySelectorAll('li');
        for (const li of lis) {
          if (!li.classList.contains('project-empty') && !li.textContent.includes('loading')) return true;
        }
        return false;
      }, { timeout: 7000 });
      projectsRenderedMs = Date.now() - t0;
    } catch (e) {
      projectsRenderedMs = null;
    }

    // Surface list
    let surfacesRenderedMs = null;
    try {
      await page.waitForFunction(() => {
        const ul = document.querySelector('#surface-list');
        if (!ul) return false;
        const lis = ul.querySelectorAll('li');
        for (const li of lis) {
          if (!li.classList.contains('surface-empty') && !li.textContent.includes('loading')) return true;
        }
        return false;
      }, { timeout: 7000 });
      surfacesRenderedMs = Date.now() - t0;
    } catch (e) {
      surfacesRenderedMs = null;
    }

    // Conn status
    const connText = await page.locator('#conn-status .conn-label').textContent().catch(() => null);
    const projectCount = await page.locator('#project-list li').count();
    const surfaceCount = await page.locator('#surface-list li').count();

    // FCP via performance timing
    const fcp = await page.evaluate(() => {
      const entries = performance.getEntriesByType('paint');
      const fcpEntry = entries.find(e => e.name === 'first-contentful-paint');
      return fcpEntry ? fcpEntry.startTime : null;
    });

    record('1_cold_load', {
      nav_http: navResp ? navResp.status() : null,
      dom_content_loaded_ms: tDom,
      projects_rendered_ms: projectsRenderedMs,
      surfaces_rendered_ms: surfacesRenderedMs,
      conn_status: connText,
      project_count: projectCount,
      surface_count: surfaceCount,
      fcp_ms: fcp,
      pass_3s_paint: fcp !== null && fcp < 3000,
      pass_5s_projects: projectsRenderedMs !== null && projectsRenderedMs < 5000,
    });
    if (projectsRenderedMs) out.click_latencies_ms.push({ action: 'cold:projects-load', ms: projectsRenderedMs });

    await page.screenshot({ path: '/tmp/wave15-01-cold.png' });
    await ctx.close();
  }

  // ========== SCENARIO 2: Project-detail click ==========
  {
    const { ctx, page } = await newPage(browser);
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      const ul = document.querySelector('#project-list');
      return ul && Array.from(ul.querySelectorAll('li')).some(li => !li.classList.contains('project-empty'));
    }, { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(300);

    // Identify first clickable project
    const projects = await page.locator('#project-list li:not(.project-empty)').all();
    const projCount = projects.length;
    let clickLatency = null;
    let detailVisible = false;
    let firstProjLabel = null;
    if (projects.length > 0) {
      firstProjLabel = (await projects[0].textContent() || '').trim().slice(0, 80);
      const t = Date.now();
      await projects[0].click().catch(() => {});
      // After project click, expect surface-list to refresh / project to be "active"
      try {
        await page.waitForFunction(() => {
          const ul = document.querySelector('#project-list');
          if (!ul) return false;
          return !!ul.querySelector('li.is-active, li.active, li[aria-current], li.selected');
        }, { timeout: 3000 });
        detailVisible = true;
      } catch (e) {
        detailVisible = false;
      }
      clickLatency = Date.now() - t;
    }

    record('2_project_click', {
      project_count: projCount,
      first_project: firstProjLabel,
      click_latency_ms: clickLatency,
      detail_marker_visible: detailVisible,
      pass_1s: clickLatency !== null && clickLatency < 1000,
    });
    if (clickLatency !== null) out.click_latencies_ms.push({ action: 'project-click', ms: clickLatency });
    await page.screenshot({ path: '/tmp/wave15-02-project.png' });
    await ctx.close();
  }

  // ========== SCENARIO 3: Surface (task / chat) click ==========
  {
    const { ctx, page } = await newPage(browser);
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      const ul = document.querySelector('#surface-list');
      return ul && Array.from(ul.querySelectorAll('li')).some(li => !li.classList.contains('surface-empty'));
    }, { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(300);

    const surfaces = page.locator('#surface-list li:not(.surface-empty)');
    const surfCount = await surfaces.count();
    let clickLatency = null;
    let transcriptRendered = false;
    let inputEnabled = false;
    let firstSurfLabel = null;

    if (surfCount > 0) {
      const target = surfaces.first();
      firstSurfLabel = (await target.textContent() || '').trim().slice(0, 80);
      const t = Date.now();
      await target.click().catch(() => {});

      // Wait for pane title to change
      try {
        await page.waitForFunction(() => {
          const t = document.querySelector('#pane-title');
          return t && t.textContent && !/Select a surface/.test(t.textContent);
        }, { timeout: 3000 });
        transcriptRendered = true;
      } catch (e) {}
      clickLatency = Date.now() - t;

      inputEnabled = await page.locator('#send-input').isEnabled().catch(() => false);
    }

    record('3_surface_click', {
      surface_count: surfCount,
      first_surface: firstSurfLabel,
      click_latency_ms: clickLatency,
      transcript_rendered: transcriptRendered,
      send_input_enabled: inputEnabled,
      pass_1s: clickLatency !== null && clickLatency < 1000,
    });
    if (clickLatency !== null) out.click_latencies_ms.push({ action: 'surface-click', ms: clickLatency });
    await page.screenshot({ path: '/tmp/wave15-03-surface.png' });
    await ctx.close();
  }

  // ========== SCENARIO 4: Multi-tab switch ==========
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const pageA = await ctx.newPage();
    const pageB = await ctx.newPage();
    const pageC = await ctx.newPage();

    for (const p of [pageA, pageB, pageC]) {
      p.on('console', msg => {
        if (msg.type() === 'error') out.console_errors.push({ tab: 'multi', text: msg.text() });
      });
      await p.goto(UI, { waitUntil: 'domcontentloaded' });
    }

    // Settle all 3
    await Promise.all([pageA, pageB, pageC].map(p =>
      p.waitForFunction(() => {
        const ul = document.querySelector('#surface-list');
        return ul && Array.from(ul.querySelectorAll('li')).some(li => !li.classList.contains('surface-empty'));
      }, { timeout: 8000 }).catch(() => null)
    ));

    // Click surface in pageA, B, C and time each
    const tabClickLatencies = [];
    for (const [idx, p] of [pageA, pageB, pageC].entries()) {
      await p.bringToFront();
      const surfaces = p.locator('#surface-list li:not(.surface-empty)');
      const cnt = await surfaces.count();
      if (cnt === 0) { tabClickLatencies.push(null); continue; }
      // pick a different surface per tab to vary load
      const target = surfaces.nth(Math.min(idx, cnt - 1));
      const t = Date.now();
      await target.click().catch(() => {});
      try {
        await p.waitForFunction(() => {
          const t = document.querySelector('#pane-title');
          return t && t.textContent && !/Select a surface/.test(t.textContent);
        }, { timeout: 3000 });
      } catch (e) {}
      tabClickLatencies.push(Date.now() - t);
    }

    // Rapid tab switch — click in A then immediately C
    const switchStart = Date.now();
    await pageA.bringToFront();
    await pageA.locator('#surface-list li:not(.surface-empty)').nth(1).click().catch(() => {});
    await pageC.bringToFront();
    await pageC.locator('#surface-list li:not(.surface-empty)').nth(2).click().catch(() => {});
    const switchElapsed = Date.now() - switchStart;

    record('4_multi_tab', {
      tabs: 3,
      tab_click_latencies_ms: tabClickLatencies,
      rapid_switch_total_ms: switchElapsed,
      pass_all_under_1s: tabClickLatencies.every(v => v !== null && v < 1000),
    });
    tabClickLatencies.forEach(ms => {
      if (ms !== null) out.click_latencies_ms.push({ action: 'multi-tab-surface', ms });
    });
    await pageA.screenshot({ path: '/tmp/wave15-04-tabA.png' });
    await ctx.close();
  }

  // ========== SCENARIO 5: Inbox deep-link + interaction ==========
  {
    const { ctx, page } = await newPage(browser);
    const t0 = Date.now();
    const resp = await page.goto(BASE + '/ui/inbox', { waitUntil: 'domcontentloaded' });
    const navStatus = resp ? resp.status() : null;
    // Inbox UI may live as a panel or modal — look for inbox indicators
    await page.waitForTimeout(2000);

    // What URL did we end up at? Did it route to a real inbox view?
    const finalUrl = page.url();
    const bodyText = (await page.locator('body').textContent() || '').slice(0, 400);

    // Try to find an inbox item heuristically
    const possibleInbox = await page.evaluate(() => {
      // Look for list items with subject/preview shape
      const all = Array.from(document.querySelectorAll('[class*=inbox], [data-inbox], #inbox, [data-testid*=inbox]'));
      return {
        inboxNodeCount: all.length,
        inboxClassNames: all.slice(0, 5).map(n => n.className || n.id),
      };
    });

    // Try clicking project-rail to see if it surfaces inbox items as part of rollup
    const rollupVisible = await page.locator('text=/inbox/i').count();

    record('5_inbox', {
      nav_status: navStatus,
      cold_load_ms: Date.now() - t0,
      final_url: finalUrl,
      body_preview: bodyText,
      possible_inbox_nodes: possibleInbox,
      rollup_inbox_count: rollupVisible,
    });
    await page.screenshot({ path: '/tmp/wave15-05-inbox.png' });
    await ctx.close();
  }

  // ========== SCENARIO 6: Error state UX (bogus API) ==========
  {
    const { ctx, page } = await newPage(browser);
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);

    // Issue a bogus API call from page context — simulate a backend error
    const result = await page.evaluate(async (base) => {
      const t = Date.now();
      const r = await fetch(base + '/api/v1/this-does-not-exist');
      const text = await r.text();
      return {
        status: r.status,
        elapsed_ms: Date.now() - t,
        content_type: r.headers.get('content-type'),
        body: text.slice(0, 400),
      };
    }, BASE);

    // Bogus chat surface
    const bogusChat = await page.evaluate(async (base) => {
      const r = await fetch(base + '/api/v1/chat/__bogus__/messages?limit=10');
      const text = await r.text();
      return { status: r.status, body: text.slice(0, 400) };
    }, BASE);

    record('6_error_envelope', {
      bogus_path: result,
      bogus_surface_messages: bogusChat,
      pass_typed_envelope: result.body.includes('"error"') && result.body.includes('"code"'),
    });
    await ctx.close();
  }

  // ========== SCENARIO 7: SSE refresh / event stream behavior ==========
  {
    const { ctx, page } = await newPage(browser);
    const sseEvents = [];
    const sseRequests = [];
    page.on('request', req => {
      if (req.url().includes('/events')) {
        sseRequests.push({ url: req.url(), resource: req.resourceType(), method: req.method() });
      }
    });
    page.on('response', async resp => {
      if (resp.url().includes('/events') && resp.url().includes('/api/v1/events')) {
        sseEvents.push({ url: resp.url(), status: resp.status(), ct: resp.headers()['content-type'] });
      }
    });

    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(8000); // give SSE / poll time to fire

    // Inspect EventSource state from window
    const sseState = await page.evaluate(() => {
      // Try multiple possible global hooks
      const candidates = ['__sseState', '__sse', 'window.eventSource'];
      const out = {};
      for (const k of candidates) {
        try { out[k] = typeof window[k]; } catch (e) {}
      }
      out.hasEventSource = 'EventSource' in window;
      // Check for visible "connecting" / live state
      const connEl = document.querySelector('#conn-status');
      out.conn_class = connEl ? connEl.className : null;
      out.conn_text = connEl ? connEl.textContent.trim() : null;
      return out;
    });

    record('7_sse', {
      sse_requests: sseRequests.slice(0, 5),
      sse_responses: sseEvents.slice(0, 5),
      conn_state: sseState,
      sse_seen: sseRequests.length > 0,
    });
    await ctx.close();
  }

  // ========== SCENARIO 8: Warm reload ==========
  {
    const { ctx, page } = await newPage(browser);
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      const ul = document.querySelector('#surface-list');
      return ul && Array.from(ul.querySelectorAll('li')).some(li => !li.classList.contains('surface-empty'));
    }, { timeout: 8000 }).catch(() => {});

    const t = Date.now();
    await page.reload({ waitUntil: 'domcontentloaded' });
    const reloadMs = Date.now() - t;
    // Surfaces should be present quickly via cache
    let surfRenderedMs = null;
    try {
      await page.waitForFunction(() => {
        const ul = document.querySelector('#surface-list');
        return ul && Array.from(ul.querySelectorAll('li')).some(li => !li.classList.contains('surface-empty'));
      }, { timeout: 5000 });
      surfRenderedMs = Date.now() - t;
    } catch (e) {}

    record('8_warm_reload', {
      reload_dom_ms: reloadMs,
      warm_surfaces_ms: surfRenderedMs,
      pass_500ms_reload: reloadMs < 500,
    });
    if (surfRenderedMs) out.click_latencies_ms.push({ action: 'warm-reload', ms: surfRenderedMs });
    await ctx.close();
  }

  await browser.close();

  // Aggregate
  const ms = out.click_latencies_ms.map(x => x.ms).filter(v => typeof v === 'number').sort((a, b) => a - b);
  if (ms.length) {
    out.latency_summary = {
      n: ms.length,
      median: ms[Math.floor(ms.length / 2)],
      p95: ms[Math.floor(ms.length * 0.95)] || ms[ms.length - 1],
      max: ms[ms.length - 1],
      over_1s_count: ms.filter(v => v > 1000).length,
      over_250ms_count: ms.filter(v => v > 250).length,
    };
  }

  fs.writeFileSync('/tmp/wave15-results.json', JSON.stringify(out, null, 2));
  console.log('\n\n=== SUMMARY ===');
  console.log(JSON.stringify(out.latency_summary, null, 2));
  console.log('console_errors:', out.console_errors.length);
  console.log('network_errors:', out.network_errors.length);
  console.log('Results: /tmp/wave15-results.json');
}

run().catch(e => {
  console.error('FATAL', e);
  fs.writeFileSync('/tmp/wave15-results.json', JSON.stringify({ ...out, fatal: e.message }, null, 2));
  process.exit(1);
});
