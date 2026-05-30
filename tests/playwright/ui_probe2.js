const { chromium } = require('@playwright/test');
const fs = require('fs');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

async function screenshot(page, name) {
  const path = `/tmp/ui_${name}.png`;
  await page.screenshot({ path, fullPage: false }).catch(() => {});
  console.log(`[SS] ${path}`);
  return path;
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  // Load UI
  const t0 = Date.now();
  await page.goto(UI, { waitUntil: 'load' });
  const ttfp = Date.now() - t0;
  // Wait for actual content (not full networkidle which hangs)
  await page.waitForSelector('li', { timeout: 10000 }).catch(() => {});
  const tti = Date.now() - t0;
  console.log(`TTFP (load event): ${ttfp}ms`);
  console.log(`TTI (first li): ${tti}ms`);

  // STEP A: Check what "online" status and session IDs look like
  const allText = await page.evaluate(() => document.body.innerText);
  console.log(`\nFull page text:\n${allText}\n`);

  // STEP B: Click the first session in rail
  const t1 = Date.now();
  const firstSession = page.locator('li').nth(1); // skip surface-group header
  await firstSession.click({ timeout: 5000 }).catch(e => console.log(`First session click error: ${e.message}`));
  const clickElapsed = Date.now() - t1;
  console.log(`Click first session: ${clickElapsed}ms`);

  // Wait for chat area to load
  await page.waitForTimeout(2000);
  await screenshot(page, '07_session_clicked');

  const afterClickText = await page.evaluate(() => document.body.innerText);
  console.log(`\nAfter click text:\n${afterClickText.substring(0, 600)}\n`);

  // Check URL
  console.log(`URL after click: ${page.url()}`);

  // STEP C: Check if message input exists
  const inputEl = await page.locator('input[type="text"], textarea, [contenteditable="true"]').count();
  console.log(`Input elements: ${inputEl}`);

  const sendBtn = await page.locator('button:has-text("Send"), button[aria-label*="send"]').count();
  console.log(`Send button: ${sendBtn}`);

  // Try typing and sending a message
  if (inputEl > 0 && sendBtn > 0) {
    const inputSelector = 'input[type="text"], textarea, [contenteditable="true"]';
    const t2 = Date.now();
    await page.fill(inputSelector, 'hello from probe test').catch(e => console.log(`Fill error: ${e.message}`));
    await screenshot(page, '08_message_typed');
    const t3 = Date.now();
    await page.click('button:has-text("Send")').catch(e => console.log(`Send click error: ${e.message}`));
    const sendElapsed = Date.now() - t3;
    console.log(`Send button click: ${sendElapsed}ms`);
    await page.waitForTimeout(3000);
    await screenshot(page, '09_after_send');
    const afterSend = await page.evaluate(() => document.body.innerText);
    console.log(`\nAfter send text:\n${afterSend.substring(0, 800)}\n`);
    const hasProbeMsg = afterSend.includes('hello from probe test');
    const hasReply = afterSend.toLowerCase().includes('reply') || afterSend.length > (afterClickText.length + 100);
    console.log(`Message appears in UI: ${hasProbeMsg}`);
    console.log(`Possible reply appeared: ${hasReply}`);
  } else {
    console.log('No message input found — checking for loading or error state');
    // Check for error or loading state
    const loadState = await page.evaluate(() => {
      const spinner = document.querySelector('[class*="spinner"], [class*="loading"]');
      const error = document.querySelector('[class*="error"], [role="alert"]');
      const noSession = document.querySelector('[class*="empty"], [class*="placeholder"]');
      return {
        spinner: spinner?.className || null,
        error: error?.textContent?.trim().substring(0, 100) || null,
        empty: noSession?.textContent?.trim().substring(0, 50) || null
      };
    });
    console.log(`Page state: ${JSON.stringify(loadState)}`);
  }

  // STEP D: API sessions - proper ID extraction
  const sessionsJson = await page.evaluate(async (base) => {
    const r = await fetch(base + '/api/v1/sessions?limit=3');
    return await r.json();
  }, BASE);
  console.log(`\nSessions API raw: ${JSON.stringify(sessionsJson).substring(0, 600)}`);

  // Find proper session name/id field
  const sessionList = sessionsJson.sessions || sessionsJson.items || sessionsJson.data || (Array.isArray(sessionsJson) ? sessionsJson : []);
  if (sessionList.length > 0) {
    const s = sessionList[0];
    console.log(`Session fields: ${Object.keys(s).join(', ')}`);
    const sid = s.name || s.id || s.session_id;
    console.log(`Using session id/name: ${sid}`);

    // Messages latency
    const msgT = Date.now();
    const msgResp = await page.evaluate(async (args) => {
      const { base, sid } = args;
      const t = Date.now();
      const r = await fetch(`${base}/api/v1/sessions/${sid}/messages`);
      const elapsed = Date.now() - t;
      const body = await r.json().catch(() => null);
      return { status: r.status, elapsed, body: JSON.stringify(body).substring(0, 400) };
    }, { base: BASE, sid });
    console.log(`\nMessages ${sid}: ${JSON.stringify(msgResp)}`);
    
    // include_thinking test (#2160)
    const thinkingResp = await page.evaluate(async (args) => {
      const { base, sid } = args;
      const t = Date.now();
      const r = await fetch(`${base}/api/v1/sessions/${sid}/messages?include_thinking=true`);
      const elapsed = Date.now() - t;
      const body = await r.json().catch(() => null);
      const msgs = Array.isArray(body) ? body : (body?.messages || body?.items || []);
      const hasThinkingEnvelope = msgs.some(m => m.type === 'thinking' || m.thinking !== undefined || 
        (Array.isArray(m.content) && m.content.some(c => c.type === 'thinking')));
      return { status: r.status, elapsed, hasThinkingEnvelope, msgCount: msgs.length, sample: JSON.stringify(body).substring(0, 300) };
    }, { base: BASE, sid });
    console.log(`\ninclude_thinking=true: ${JSON.stringify(thinkingResp)}`);
  }

  // STEP E: Task lifecycle - POST create task
  const taskCreateResp = await page.evaluate(async (base) => {
    const r = await fetch(base + '/api/v1/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'ui-probe-test', project: 'pollypm' })
    });
    const body = await r.json().catch(() => null);
    return { status: r.status, body: JSON.stringify(body).substring(0, 200) };
  }, BASE);
  console.log(`\nPOST /api/v1/tasks: ${JSON.stringify(taskCreateResp)}`);

  // Try with project_id
  const taskCreate2 = await page.evaluate(async (base) => {
    const r = await fetch(base + '/api/v1/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'ui-probe-test-2', project_id: 'pollypm' })
    });
    const body = await r.json().catch(() => null);
    return { status: r.status, body: JSON.stringify(body).substring(0, 200) };
  }, BASE);
  console.log(`POST /api/v1/tasks (project_id): ${JSON.stringify(taskCreate2)}`);

  // STEP F: Check task list for dwell_seconds and stuck_reason (#2163)
  const taskListRaw = await page.evaluate(async (base) => {
    const r = await fetch(base + '/api/v1/tasks?limit=1');
    const body = await r.json();
    const tasks = body.items || body.tasks || (Array.isArray(body) ? body : []);
    if (tasks.length) {
      return { keys: Object.keys(tasks[0]).join(', '), first: JSON.stringify(tasks[0]).substring(0, 500) };
    }
    return { empty: true };
  }, BASE);
  console.log(`\nTask list raw: ${JSON.stringify(taskListRaw)}`);

  // Check /api/v1/tasks/{id} for individual task
  const taskDetailRaw = await page.evaluate(async (base) => {
    try {
      const r = await fetch(base + '/api/v1/tasks?limit=1');
      const body = await r.json();
      const tasks = body.items || body.tasks || (Array.isArray(body) ? body : []);
      if (!tasks.length) return { empty: true };
      const tid = tasks[0].task_id;
      if (!tid) return { no_task_id: true, keys: Object.keys(tasks[0]).join(', ') };
      const r2 = await fetch(base + '/api/v1/tasks/' + encodeURIComponent(tid));
      const body2 = await r2.json();
      return { status: r2.status, keys: Object.keys(body2).join(', '), 
        has_dwell: 'dwell_seconds' in body2, has_stuck: 'stuck_reason' in body2,
        sample: JSON.stringify(body2).substring(0, 400) };
    } catch(e) { return { error: String(e) }; }
  }, BASE);
  console.log(`Task detail raw: ${JSON.stringify(taskDetailRaw)}`);

  // STEP G: Dashboard p95 latency — run 3 times
  console.log('\n=== Dashboard latency (3 runs) ===');
  for (let i = 0; i < 3; i++) {
    const r = await page.evaluate(async (base) => {
      const t = Date.now();
      const resp = await fetch(base + '/api/v1/dashboard');
      const elapsed = Date.now() - t;
      return { elapsed, status: resp.status };
    }, BASE);
    console.log(`  Dashboard run ${i+1}: ${r.elapsed}ms (status ${r.status})`);
  }

  await screenshot(page, '10_final');
  await browser.close();
}

run().catch(e => { console.error('PROBE2 ERROR:', e); process.exit(1); });
