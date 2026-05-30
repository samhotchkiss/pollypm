/*
 * Wave-1.5 final probe — surface-rail real shape, click latencies on
 * actual clickable LIs, multi-tab perf, chat-surface availability.
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

  // A. Surface-rail FULL render: wait for actual clickable surface items
  // (not just any LI). Measure how long until a chat-surface OR task-surface
  // class appears.
  {
    const times = [];
    const noChat = [];
    for (let i = 0; i < 5; i++) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const t0 = Date.now();
      await page.goto(UI, { waitUntil: 'domcontentloaded' });
      let firstSurface = null, firstChat = null, taskSurfaceCount = 0, chatSurfaceCount = 0;
      try {
        await page.waitForFunction(() => {
          return !!document.querySelector('#surface-list .task-surface, #surface-list .chat-surface, #surface-list li[data-session]');
        }, { timeout: 10000 });
        firstSurface = Date.now() - t0;
      } catch (e) {}
      try {
        await page.waitForFunction(() => {
          return !!document.querySelector('#surface-list .chat-surface, #surface-list li[data-session]');
        }, { timeout: 12000 });
        firstChat = Date.now() - t0;
      } catch (e) {
        noChat.push(i);
      }
      const counts = await page.evaluate(() => ({
        task: document.querySelectorAll('#surface-list .task-surface').length,
        chat: document.querySelectorAll('#surface-list .chat-surface, #surface-list li[data-session]').length,
        loading_text: !!Array.from(document.querySelectorAll('#surface-list li')).find(li => /loading chat surfaces/.test(li.textContent)),
        error_text: !!Array.from(document.querySelectorAll('#surface-list li')).find(li => /chat unavailable/.test(li.textContent)),
      }));
      taskSurfaceCount = counts.task;
      chatSurfaceCount = counts.chat;
      times.push({ trial: i, first_surface_ms: firstSurface, first_chat_ms: firstChat, task_n: taskSurfaceCount, chat_n: chatSurfaceCount, chat_loading_stuck: counts.loading_text, chat_error: counts.error_text });
      await ctx.close();
    }
    rec('A_rail_complete_5x', {
      trials: times,
      chat_surfaces_never_rendered_count: noChat.length,
    });
    times.forEach(t => {
      if (t.first_surface_ms) out.latencies.push({ action: 'first-surface', ms: t.first_surface_ms });
    });
  }

  // B. Click latency — click 5 task-surface items, time pane-title change
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(UI, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#surface-list .task-surface', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(300);

    const trials = [];
    const items = page.locator('#surface-list .task-surface');
    const n = await items.count();
    for (let i = 0; i < Math.min(5, n); i++) {
      const target = items.nth(i);
      const dataTask = await target.getAttribute('data-task');
      // capture pane-title BEFORE click to detect change
      const prevTitle = (await page.locator('#pane-title').textContent()) || '';
      const t = Date.now();
      await target.click();
      let ms = null, changed = false;
      try {
        await page.waitForFunction((prev) => {
          const t = document.querySelector('#pane-title');
          return t && t.textContent && t.textContent.trim() !== prev.trim()
            && !/Select a surface/.test(t.textContent);
        }, prevTitle, { timeout: 3000 });
        ms = Date.now() - t;
        changed = true;
      } catch (e) {
        ms = Date.now() - t;
      }
      const newTitle = (await page.locator('#pane-title').textContent()) || '';
      trials.push({ task: dataTask, click_ms: ms, title_changed: changed, new_title: newTitle.trim().slice(0, 60) });
      await page.waitForTimeout(150);
    }
    rec('B_task_click_5x', {
      trials,
      pass_under_1s: trials.every(t => t.click_ms !== null && t.click_ms < 1000),
    });
    trials.forEach(t => { if (t.click_ms) out.latencies.push({ action: 'task-click', ms: t.click_ms }); });
    await ctx.close();
  }

  // C. Inbox actions: load /ui/inbox, find an inbox-item, click + observe state
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(UI + 'inbox', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.inbox-view', { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(1500);

    const shape = await page.evaluate(() => {
      // enumerate candidate inbox items
      const candidates = Array.from(document.querySelectorAll('.inbox-view *')).filter(n => {
        const c = n.className || '';
        return typeof c === 'string' && /inbox-(item|row|card|entry)/.test(c);
      });
      return {
        candidate_count: candidates.length,
        classes_sample: candidates.slice(0, 5).map(n => n.className),
        inbox_view_html_len: document.querySelector('.inbox-view')?.innerHTML.length || 0,
        all_classes_in_inbox: Array.from(new Set(Array.from(document.querySelectorAll('.inbox-view *')).map(n => n.className).filter(c => typeof c === 'string' && c)))
          .slice(0, 30),
      };
    });

    // attempt to click first one
    let clickResult = null;
    if (shape.candidate_count > 0) {
      const sel = '.inbox-view .' + shape.classes_sample[0].split(/\s+/)[0];
      const t = Date.now();
      await page.locator(sel).first().click().catch(() => {});
      await page.waitForTimeout(400);
      clickResult = {
        click_ms: Date.now() - t,
        url_after: page.url(),
      };
    }

    rec('C_inbox_interaction', { shape, clickResult });
    if (clickResult && clickResult.click_ms) out.latencies.push({ action: 'inbox-item-click', ms: clickResult.click_ms });
    await ctx.close();
  }

  await browser.close();

  const ms = out.latencies.map(x => x.ms).filter(v => typeof v === 'number').sort((a, b) => a - b);
  if (ms.length) {
    out.summary = {
      n: ms.length,
      median: ms[Math.floor(ms.length / 2)],
      p95: ms[Math.min(ms.length - 1, Math.floor(ms.length * 0.95))],
      max: ms[ms.length - 1],
      over_1s: ms.filter(v => v > 1000).length,
      over_250ms: ms.filter(v => v > 250).length,
    };
  }
  fs.writeFileSync('/tmp/wave15-final.json', JSON.stringify(out, null, 2));
  console.log('\n=== FINAL SUMMARY ===\n', JSON.stringify(out.summary, null, 2));
}
run().catch(e => { console.error('FATAL', e); fs.writeFileSync('/tmp/wave15-final.json', JSON.stringify({ ...out, fatal: e.message }, null, 2)); process.exit(1); });
