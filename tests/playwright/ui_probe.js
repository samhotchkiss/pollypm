// Black-box UI probe
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

const findings = [];
const clickTimings = [];

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

  // STEP 1: Navigate — TTFP/TTI
  console.log('\n=== STEP 1: Load /ui/ ===');
  const t0 = Date.now();
  await page.goto(UI, { waitUntil: 'domcontentloaded' });
  const ttfp = Date.now() - t0;
  await page.waitForLoadState('networkidle').catch(() => {});
  const tti = Date.now() - t0;
  console.log(`TTFP: ${ttfp}ms`);
  console.log(`TTI: ${tti}ms`);
  if (ttfp > 1000) findings.push({ type: 'perf', title: `perf:TTFP_${ttfp}ms`, detail: `TTFP was ${ttfp}ms` });
  if (tti > 2000) findings.push({ type: 'perf', title: `perf:TTI_${tti}ms`, detail: `TTI was ${tti}ms` });

  const ss1 = await screenshot(page, '01_landing');
  const currentUrl = page.url();
  console.log(`URL after load: ${currentUrl}`);

  // STEP 2: First impression scan
  console.log('\n=== STEP 2: First impression ===');
  const pageTitle = await page.title();
  const bodyText = await page.evaluate(() => document.body.innerText);
  const allLabels = await page.evaluate(() => {
    const els = Array.from(document.querySelectorAll('h1, h2, h3, nav a, button, [role="tab"], [class*="title"]'));
    return els.map(el => el.textContent?.trim()).filter(Boolean).slice(0, 50).join(' | ');
  });
  console.log(`Title: ${pageTitle}`);
  console.log(`Body preview: ${bodyText.substring(0, 400)}`);
  console.log(`UI labels: ${allLabels}`);

  const hasWelcome = /welcome|get started|create your first|no projects yet/i.test(bodyText);
  const hasProject = /project/i.test(bodyText);
  const hasTask = /task/i.test(bodyText);
  const hasInbox = /inbox/i.test(bodyText);
  const h1Count = await page.locator('h1').count();
  console.log(`welcome/start cue: ${hasWelcome}, project: ${hasProject}, task: ${hasTask}, inbox: ${hasInbox}, h1: ${h1Count}`);

  if (!hasWelcome && h1Count === 0) {
    findings.push({
      type: 'magic-gap',
      title: 'magic-gap:no-onboarding-cue-on-landing',
      detail: `Landing page shows no "get started" / "welcome" / empty-state guidance. Body: "${bodyText.substring(0, 200)}"`,
      screenshot: ss1
    });
  }

  // STEP 3: Projects API latency
  console.log('\n=== STEP 3: API latency checks ===');

  const apiChecks = [
    '/api/v1/dashboard',
    '/api/v1/projects',
    '/api/v1/tasks',
    '/api/v1/sessions',
    '/api/v1/inbox'
  ];

  for (const ep of apiChecks) {
    const r = await page.evaluate(async (args) => {
      const { base, ep } = args;
      const t = Date.now();
      try {
        const resp = await fetch(base + ep);
        const elapsed = Date.now() - t;
        let body;
        try { body = await resp.json(); } catch(e) { body = null; }
        return {
          ep,
          status: resp.status,
          elapsed,
          preview: JSON.stringify(body).substring(0, 200)
        };
      } catch(e) {
        return { ep, error: String(e), elapsed: Date.now() - t };
      }
    }, { base: BASE, ep });
    console.log(`${ep}: status=${r.status || 'ERR'} ${r.elapsed}ms | ${(r.preview || r.error || '').substring(0, 100)}`);
    if (r.elapsed > 1000 && r.status && r.status < 500) {
      findings.push({ type: 'perf', title: `perf:api-slow:${ep}:${r.elapsed}ms`, detail: `${ep} took ${r.elapsed}ms` });
    }
  }

  // STEP 4: Task fields check (#2163 dwell_seconds + stuck_reason)
  console.log('\n=== STEP 4: TaskSummary fields ===');
  const taskFields = await page.evaluate(async (base) => {
    try {
      const r = await fetch(base + '/api/v1/tasks?limit=5');
      if (!r.ok) return { error: r.status, text: await r.text() };
      const body = await r.json();
      const tasks = Array.isArray(body) ? body : (body.tasks || body.items || body.data || []);
      if (!tasks.length) return { empty: true, rawKeys: Object.keys(body) };
      return {
        count: tasks.length,
        has_dwell_seconds: 'dwell_seconds' in tasks[0],
        has_stuck_reason: 'stuck_reason' in tasks[0],
        keys: Object.keys(tasks[0]).join(', '),
        sample_status: tasks[0].status
      };
    } catch(e) { return { error: String(e) }; }
  }, BASE);
  console.log(`Task fields: ${JSON.stringify(taskFields)}`);

  // STEP 5: Click around the UI
  console.log('\n=== STEP 5: UI clicking ===');

  // Go back to base UI
  if (!page.url().includes('/ui')) {
    await page.goto(UI, { waitUntil: 'networkidle' }).catch(() => {});
  }

  // Try clicking nav items
  const navItems = await page.locator('nav a, nav button, [role="tab"]').all();
  console.log(`Nav/tab items found: ${navItems.length}`);

  for (let i = 0; i < Math.min(navItems.length, 5); i++) {
    const label = await navItems[i].textContent().catch(() => `item-${i}`);
    const href = await navItems[i].getAttribute('href').catch(() => null);
    const t1 = Date.now();
    await navItems[i].click({ timeout: 3000 }).catch(e => {
      console.log(`Nav click failed: ${label} — ${e.message?.substring(0, 80)}`);
    });
    const elapsed = Date.now() - t1;
    const clickLabel = `nav:${(label || href || i).toString().trim().substring(0, 30)}`;
    clickTimings.push({ action: clickLabel, ms: elapsed });
    console.log(`Click ${clickLabel}: ${elapsed}ms`);
    if (elapsed > 1000) {
      findings.push({ type: 'perf', title: `perf:click-latency:${clickLabel}:${elapsed}ms`, detail: `Nav click "${label}" took ${elapsed}ms` });
    }
    await page.waitForLoadState('networkidle').catch(() => {});
  }

  await screenshot(page, '02_after_nav');

  // STEP 6: Look for project/task links in main content
  console.log('\n=== STEP 6: Project & task discovery ===');
  const linkEls = await page.locator('a').all();
  const linkData = [];
  for (const link of linkEls.slice(0, 20)) {
    const href = await link.getAttribute('href').catch(() => null);
    const text = await link.textContent().catch(() => '');
    linkData.push({ href, text: text.trim().substring(0, 40) });
  }
  console.log(`Links: ${JSON.stringify(linkData)}`);

  // Click on first project-like link
  const projectLink = page.locator('a[href*="project"]').first();
  if (await projectLink.count() > 0) {
    const href = await projectLink.getAttribute('href');
    const t1 = Date.now();
    await projectLink.click({ timeout: 5000 }).catch(e => console.log(`Project link click failed: ${e.message?.substring(0, 80)}`));
    const elapsed = Date.now() - t1;
    clickTimings.push({ action: `click-project-link:${href}`, ms: elapsed });
    console.log(`Click project link ${href}: ${elapsed}ms`);
    await page.waitForLoadState('networkidle').catch(() => {});
    const ss3 = await screenshot(page, '03_project_detail');

    // Check surfaces (tasks, chat, etc.)
    const surfaceTabs = await page.locator('[role="tab"], .tab, [class*="tab"]').all();
    console.log(`Project surface tabs: ${surfaceTabs.length}`);
    for (const tab of surfaceTabs.slice(0, 6)) {
      const txt = await tab.textContent().catch(() => '');
      const t2 = Date.now();
      await tab.click({ timeout: 3000 }).catch(e => {});
      const elapsed2 = Date.now() - t2;
      const tLabel = `tab:${txt.trim().substring(0, 20)}`;
      clickTimings.push({ action: tLabel, ms: elapsed2 });
      console.log(`Tab click "${txt.trim()}": ${elapsed2}ms`);
      await page.waitForLoadState('networkidle').catch(() => {});
    }
    await screenshot(page, '04_project_tabs');
  } else {
    console.log('No project links found — checking for task list instead');
    const taskLink = page.locator('a[href*="task"]').first();
    if (await taskLink.count() > 0) {
      const href = await taskLink.getAttribute('href');
      await taskLink.click({ timeout: 5000 }).catch(() => {});
      await page.waitForLoadState('networkidle').catch(() => {});
      await screenshot(page, '03_task_detail');
    }
  }

  // STEP 7: Send message to session
  console.log('\n=== STEP 7: Message sending ===');

  const sessionsData = await page.evaluate(async (base) => {
    try {
      const r = await fetch(base + '/api/v1/sessions?limit=5');
      if (!r.ok) return { error: r.status };
      return await r.json();
    } catch(e) { return { error: String(e) }; }
  }, BASE);
  console.log(`Sessions: ${JSON.stringify(sessionsData).substring(0, 400)}`);

  let testSessionId = null;
  if (sessionsData && !('error' in sessionsData)) {
    const list = Array.isArray(sessionsData) ? sessionsData : (sessionsData.sessions || sessionsData.items || sessionsData.data || []);
    if (list.length > 0) {
      testSessionId = list[0].id || list[0].session_id;
      console.log(`Found session: ${testSessionId}`);

      // Messages endpoint latency (#2164)
      const msgT = Date.now();
      const msgData = await page.evaluate(async (args) => {
        const { base, sid } = args;
        const t = Date.now();
        try {
          const r = await fetch(`${base}/api/v1/sessions/${sid}/messages`);
          const elapsed = Date.now() - t;
          const body = await r.json().catch(() => null);
          return { status: r.status, elapsed, body: JSON.stringify(body).substring(0, 300) };
        } catch(e) { return { error: String(e) }; }
      }, { base: BASE, sid: testSessionId });
      console.log(`Messages for ${testSessionId}: ${JSON.stringify(msgData)}`);

      if (msgData && msgData.elapsed > 1000) {
        findings.push({
          type: 'perf',
          title: `perf:messages-latency:${msgData.elapsed}ms`,
          detail: `GET /sessions/${testSessionId}/messages took ${msgData.elapsed}ms. Issue #2164 targeted ≥1.4s floor`
        });
      }

      // Try sending a message
      const sendT = Date.now();
      const sendResp = await page.evaluate(async (args) => {
        const { base, sid } = args;
        const t = Date.now();
        try {
          const r = await fetch(`${base}/api/v1/sessions/${sid}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: 'probe test message', role: 'user' })
          });
          const elapsed = Date.now() - t;
          const body = await r.json().catch(() => null);
          return { status: r.status, elapsed, body: JSON.stringify(body).substring(0, 200) };
        } catch(e) { return { error: String(e) }; }
      }, { base: BASE, sid: testSessionId });
      console.log(`POST message: ${JSON.stringify(sendResp)}`);
    }
  }

  // STEP 8: Check rail cards clickable (#2142)
  console.log('\n=== STEP 8: Rail card clickability (#2142) ===');
  // Navigate to homepage to see rail
  await page.goto(UI, { waitUntil: 'networkidle' }).catch(() => {});
  const railSS = await screenshot(page, '05_rail');

  const cardInfo = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('[class*="card"], [class*="item"], li'));
    return cards.slice(0, 15).map(el => {
      const style = window.getComputedStyle(el);
      const isClickable = style.cursor === 'pointer' ||
        el.tagName === 'A' ||
        el.hasAttribute('onclick') ||
        el.getAttribute('role') === 'button' ||
        el.closest('a') !== null;
      return {
        tag: el.tagName,
        cls: el.className.substring(0, 50),
        cursor: style.cursor,
        isClickable,
        text: el.textContent?.trim().substring(0, 30)
      };
    });
  });
  console.log(`Card info: ${JSON.stringify(cardInfo)}`);

  // check for visual affordance mismatch: looks-like-card but not clickable
  const mismatchCards = cardInfo.filter(c => 
    (c.cls.includes('card') || c.cls.includes('item')) && 
    !c.isClickable && 
    c.text && c.text.length > 3
  );
  if (mismatchCards.length > 0) {
    findings.push({
      type: 'bug',
      title: `bug:rail-cards-not-clickable:${mismatchCards.length}-cards`,
      detail: `${mismatchCards.length} card-like elements found with non-clickable cursor. First: "${mismatchCards[0].cls}" cursor="${mismatchCards[0].cursor}" text="${mismatchCards[0].text}". Issue #2142 territory.`,
      screenshot: railSS
    });
  }

  // STEP 9: Error states & spinners
  console.log('\n=== STEP 9: Error states & spinners ===');
  const spinners = await page.locator('[class*="spinner"], [class*="loading"], [class*="skeleton"], [aria-busy="true"]').count();
  const errorMsgs = await page.locator('[class*="error"], [role="alert"], [class*="alert"]').count();
  console.log(`Spinners/loading: ${spinners}, Errors/alerts: ${errorMsgs}`);
  if (spinners > 0) {
    const spinnerText = await page.locator('[class*="spinner"], [class*="loading"]').first().textContent().catch(() => '');
    findings.push({ type: 'bug', title: `bug:unresolved-spinner`, detail: `${spinners} spinner/loading elements still visible after networkidle. First: "${spinnerText}"` });
  }

  // STEP 10: Mobile
  console.log('\n=== STEP 10: Mobile viewport ===');
  await context.close();
  const mobileCtx = await browser.newContext({ viewport: { width: 360, height: 800 } });
  const mobilePg = await mobileCtx.newPage();
  const mT = Date.now();
  await mobilePg.goto(UI, { waitUntil: 'networkidle' }).catch(() => {});
  const mobileTTI = Date.now() - mT;
  console.log(`Mobile TTI: ${mobileTTI}ms`);
  await mobilePg.screenshot({ path: '/tmp/ui_06_mobile.png', fullPage: false });

  const overflow = await mobilePg.evaluate(() => ({
    docWidth: document.documentElement.scrollWidth,
    viewWidth: window.innerWidth,
    overflowing: document.documentElement.scrollWidth > window.innerWidth + 5
  }));
  console.log(`Mobile overflow: ${JSON.stringify(overflow)}`);
  if (overflow.overflowing) {
    findings.push({
      type: 'ux',
      title: `ux:mobile-horizontal-overflow:doc=${overflow.docWidth}px:view=${overflow.viewWidth}px`,
      detail: `At 360px viewport page content is ${overflow.docWidth}px wide. Horizontal scroll visible on mobile.`,
      screenshot: '/tmp/ui_06_mobile.png'
    });
  }

  const mobileBodyText = await mobilePg.evaluate(() => document.body.innerText.substring(0, 300));
  console.log(`Mobile body text: ${mobileBodyText.substring(0, 150)}`);
  await mobileCtx.close();

  // include_thinking check (#2160)
  console.log('\n=== STEP 11: include_thinking check (#2160) ===');
  if (testSessionId) {
    const thinkingResp = await page.evaluate ? null : null;
    // We already closed context, skip this step
    console.log('(skipped — context closed)');
  }

  await browser.close();

  // Final summary
  console.log('\n\n===== FINAL SUMMARY =====');
  console.log(`TTFP: ${ttfp}ms`);
  console.log(`TTI: ${tti}ms`);
  console.log('\nClick timings:');
  clickTimings.forEach(t => console.log(`  ${t.action}: ${t.ms}ms${t.ms > 1000 ? ' *** >1s ***' : ''}`));
  console.log('\nFindings:');
  findings.forEach((f, i) => console.log(`  ${i+1}. [${f.type}] ${f.title}\n     ${f.detail}`));

  fs.writeFileSync('/tmp/ui_probe_results.json', JSON.stringify({ ttfp, tti, clickTimings, findings, taskFields, sessionsData }, null, 2));
  console.log('\nResults: /tmp/ui_probe_results.json');
}

run().catch(e => { console.error('PROBE ERROR:', e); process.exit(1); });
