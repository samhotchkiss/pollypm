/*
 * Wave-1.5 follow-up — focused on:
 *  (a) precise surface-list LI clicks (not the rail title)
 *  (b) surface-rail render budget (initial spike to 5472ms was concerning)
 *  (c) multi-tab rapid switch (30s anomaly)
 *  (d) inbox /ui/inbox interaction depth (open + dismiss)
 *  (e) error-state UX in-browser — does a failed surface fetch show user-visible text?
 *  (f) deep-link /ui/inbox query handling
 */
const { chromium } = require('@playwright/test');
const fs = require('fs');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

const out = { ts: new Date().toISOString(), scenarios: {}, latencies: [] };

function rec(k, v) {
  out.scenarios[k] = v;
  console.log('\n===', k, '===');
  console.log(JSON.stringify(v, null, 2));
}

async function run() {
  const browser = await chromium.launch({ headless: true });

  // ----- Setup helper: get a real selector for a clickable surface LI -----
  // The surface-list contains <li class="surface-item"> children.

  // (1) Surface-rail render time across 5 cold loads
  {
    const times = [];
    for (let i = 0; i < 5; i++) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const t0 = Date.now();
      await page.goto(UI, { waitUntil: 'domcontentloaded' });
      try {
        await page.waitForSelector('#surface-list li:not(.surface-empty)', { timeout: 10000 });
        times.push(Date.now() - t0);
      } catch (e) {
        times.push(null);
      }
      await ctx.close();
    }
    times.sort((a, b) => a - b);
    rec('A_surface_rail_5x', {
      times_ms: times,
      median: times[2],
      max: times[4],
      pass_under_5s: times.every(t => t !== null && t < 5000),
    });
    times.forEach(ms => { if (ms) out.latencies.push({ action: 'surface-rail-cold', ms }); });
  }

  // (2) Surface click — precise selector, 5 trials
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#surface-list li:not(.surface-empty)', { timeout: 10000 });
    await page.waitForTimeout(500);

    // Find by attribute or class
    const surfStructure = await page.evaluate(() => {
      const ul = document.querySelector('#surface-list');
      const lis = Array.from(ul.querySelectorAll('li'));
      return lis.slice(0, 6).map(li => ({
        cls: li.className,
        text: li.textContent.trim().slice(0, 60),
        dataset: { ...li.dataset },
      }));
    });

    const clickLatencies = [];
    // Use first 5 actual surface items
    for (let i = 0; i < 5; i++) {
      const target = page.locator('#surface-list li.surface-item').nth(i);
      const exists = (await target.count()) > 0;
      if (!exists) { clickLatencies.push(null); continue; }
      const label = (await target.textContent()).trim().slice(0, 50);
      const t = Date.now();
      await target.click();
      try {
        await page.waitForFunction((expected) => {
          const t = document.querySelector('#pane-title');
          return t && t.textContent && !/Select a surface/.test(t.textContent);
        }, undefined, { timeout: 3000 });
        clickLatencies.push({ ms: Date.now() - t, label });
      } catch (e) {
        clickLatencies.push({ ms: Date.now() - t, label, transcript_missed: true });
      }
      // small settle before next click
      await page.waitForTimeout(150);
    }

    rec('B_surface_click_precise', {
      surf_dom_shape: surfStructure,
      click_latencies: clickLatencies,
      pass_all_under_1s: clickLatencies.every(c => c && c.ms < 1000),
    });
    clickLatencies.forEach(c => { if (c && c.ms) out.latencies.push({ action: 'surface-click-precise', ms: c.ms }); });
    await ctx.close();
  }

  // (3) Multi-tab WITHOUT bringToFront ping-pong — test true backgrounding
  {
    const ctx = await browser.newContext();
    const pages = [];
    for (let i = 0; i < 3; i++) {
      const p = await ctx.newPage();
      await p.goto(UI, { waitUntil: 'domcontentloaded' });
      await p.waitForSelector('#surface-list li.surface-item', { timeout: 8000 }).catch(() => {});
      pages.push(p);
    }
    // Bring tab 0 to front, click a surface, time response
    const trials = [];
    for (let t = 0; t < 3; t++) {
      const p = pages[t];
      await p.bringToFront();
      const target = p.locator('#surface-list li.surface-item').nth(t);
      if (!(await target.count())) { trials.push(null); continue; }
      const t0 = Date.now();
      await target.click();
      try {
        await p.waitForFunction(() => {
          const ti = document.querySelector('#pane-title');
          return ti && ti.textContent && !/Select a surface/.test(ti.textContent);
        }, { timeout: 3000 });
        trials.push({ tab: t, ms: Date.now() - t0 });
      } catch (e) {
        trials.push({ tab: t, ms: Date.now() - t0, miss: true });
      }
    }

    rec('C_multi_tab_sequential', {
      trials,
      pass_all_under_1s: trials.every(t => t && t.ms < 1000),
    });
    trials.forEach(t => { if (t && t.ms) out.latencies.push({ action: 'multi-tab-precise', ms: t.ms }); });
    await ctx.close();
  }

  // (4) Inbox: deep link, click an item, observe state change
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const t0 = Date.now();
    await page.goto(UI + 'inbox', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.inbox-view, [class*=inbox]', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const loadMs = Date.now() - t0;

    // Look for inbox items
    const inboxShape = await page.evaluate(() => {
      const items = Array.from(document.querySelectorAll('.inbox-item, [class*=inbox] li, [class*=inbox-row]'));
      return {
        item_count: items.length,
        first_cls: items[0]?.className,
        first_text: items[0]?.textContent.trim().slice(0, 100),
      };
    });

    let clickResult = null;
    if (inboxShape.item_count > 0) {
      const first = page.locator('.inbox-item, [class*=inbox-row]').first();
      const t = Date.now();
      await first.click().catch(() => {});
      await page.waitForTimeout(500);
      clickResult = {
        click_ms: Date.now() - t,
        after_url: page.url(),
        body_change_excerpt: (await page.locator('body').textContent()).slice(0, 200),
      };
    }

    rec('D_inbox_deep_link', {
      cold_load_ms: loadMs,
      inbox_shape: inboxShape,
      click_result: clickResult,
    });
    await ctx.close();
  }

  // (5) Surface error UX — kill auth (forge bad cookie) and observe UI
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#surface-list li.surface-item', { timeout: 8000 }).catch(() => {});

    // Inject a fetch override to make next surface message GET return 500
    const captured = await page.evaluate(async (base) => {
      // Find first surface name from the DOM
      const li = document.querySelector('#surface-list li.surface-item');
      const name = li?.dataset?.session || li?.dataset?.name || (li?.textContent.trim().split(/\s+/)[0]);
      // Manually request a known-bogus surface and observe API shape
      const r = await fetch(base + '/api/v1/chat/__never__/messages');
      const txt = await r.text();
      return { status: r.status, body: txt.slice(0, 300), surface_attempted: name };
    }, BASE);

    rec('E_surface_failure_envelope', captured);
    await ctx.close();
  }

  // (6) /ui/inbox URL handling — does the route load the inbox view directly?
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(UI + 'inbox', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const titleAfter = await page.locator('#pane-title').textContent().catch(() => null);
    const inboxVisible = await page.locator('.inbox-view').isVisible().catch(() => false);
    rec('F_inbox_route', {
      pane_title: titleAfter && titleAfter.trim(),
      inbox_view_visible: inboxVisible,
      url: page.url(),
    });
    await ctx.close();
  }

  await browser.close();

  // Summary
  const ms = out.latencies.map(x => x.ms).sort((a, b) => a - b);
  if (ms.length) {
    out.summary = {
      n: ms.length,
      median: ms[Math.floor(ms.length / 2)],
      p95: ms[Math.floor(ms.length * 0.95)] || ms[ms.length - 1],
      max: ms[ms.length - 1],
      over_1s: ms.filter(v => v > 1000).length,
      over_250ms: ms.filter(v => v > 250).length,
    };
  }
  fs.writeFileSync('/tmp/wave15-followup.json', JSON.stringify(out, null, 2));
  console.log('\n=== FOLLOWUP SUMMARY ===\n', JSON.stringify(out.summary, null, 2));
}
run().catch(e => { console.error('FATAL', e); fs.writeFileSync('/tmp/wave15-followup.json', JSON.stringify({...out, fatal: e.message }, null, 2)); process.exit(1); });
