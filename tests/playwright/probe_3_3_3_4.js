// Focused probe for §3.3 state indicator parity + §3.4 detail panel richness
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8765';
const UI = BASE + '/ui/';
const TOKEN = 'ysQmcaWCv7wWi3vUkz4jqcNKSItsxvN2ARRs88jNvDQ';

async function api(path) {
  const r = await fetch(BASE + path, { headers: { Authorization: 'Bearer ' + TOKEN }});
  return r.json();
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    extraHTTPHeaders: { Authorization: 'Bearer ' + TOKEN },
  });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE_ERR:', m.text()); });

  await page.goto(UI, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.waitForTimeout(4000);
  await page.screenshot({ path: '/tmp/probe334_load.png', fullPage: false });

  // Snapshot rail surface dot classes for sessions
  const sessionDots = await page.$$eval('li[data-session]', els => els.map(li => ({
    name: li.getAttribute('data-session'),
    dotClass: (li.querySelector('.surface-dot')?.className || ''),
  })));
  console.log('SESSION_DOTS_TOTAL:', sessionDots.length);
  const dotBuckets = {};
  for (const s of sessionDots) {
    const c = s.dotClass.replace('surface-dot', '').trim();
    dotBuckets[c || 'neutral'] = (dotBuckets[c || 'neutral'] || 0) + 1;
  }
  console.log('SESSION_DOT_BUCKETS:', JSON.stringify(dotBuckets));
  console.log('SESSION_SAMPLE:', JSON.stringify(sessionDots.slice(0, 8), null, 2));

  // Compare with API: session window.present/pane_dead
  const sessApi = await api('/api/v1/chat/sessions');
  const lookup = {};
  for (const s of sessApi.sessions) lookup[s.session_name] = s.window || {};
  let mismatch = 0;
  const mismatchSamples = [];
  for (const s of sessionDots) {
    const w = lookup[s.name];
    if (!w) continue;
    const expected = w.pane_dead ? 'dead' : (w.present ? 'present' : '');
    const actual = s.dotClass.replace('surface-dot', '').trim();
    if (actual !== expected) {
      mismatch++;
      if (mismatchSamples.length < 5) mismatchSamples.push({name: s.name, actual, expected, w});
    }
  }
  console.log('SESSION_PARITY_MISMATCHES:', mismatch);
  if (mismatchSamples.length) console.log('SESSION_MISMATCH_SAMPLES:', JSON.stringify(mismatchSamples, null, 2));

  // Project list — check tracked/paused glyphs
  const projItems = await page.$$eval('#project-list li, .project-item', els => els.map(li => ({
    cls: li.className,
    text: (li.textContent || '').slice(0, 120).trim(),
    data: Object.fromEntries(Array.from(li.attributes).filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value])),
  })));
  console.log('PROJECT_ITEMS_TOTAL:', projItems.length);
  console.log('PROJECT_ITEMS_SAMPLE:', JSON.stringify(projItems.slice(0, 10), null, 2));

  // Task rail — gather state indicators per task
  const taskItems = await page.$$eval('li[data-task]', els => els.map(li => ({
    key: li.getAttribute('data-task'),
    dotClass: (li.querySelector('.surface-dot')?.className || ''),
    meta: (li.querySelector('.surface-meta')?.textContent || ''),
  })));
  console.log('TASK_ITEMS_TOTAL:', taskItems.length);
  console.log('TASK_ITEMS_SAMPLE:', JSON.stringify(taskItems.slice(0, 10), null, 2));

  // Map known terminal states present
  // Drive task selection via app.selectTask. Pick test cases:
  const targets = [
    { key: 'russell/376', label: 'draft' },
    { key: 'smoketest/49', label: 'queued' },
    { key: 'russell/374', label: 'cancelled' },
    { key: 'samblog/4',   label: 'blocked' },
    { key: 'savethenovel/94', label: 'on_hold' },
    { key: 'pollypm/221', label: 'done' },
  ];

  for (const t of targets) {
    console.log('\n=== TASK: ' + t.key + ' (' + t.label + ') ===');
    const ok = await page.evaluate((k) => {
      try { window.PollyPM.selectTask(k); return true; } catch (e) { return String(e); }
    }, t.key);
    if (ok !== true) { console.log('SELECT_FAIL:', ok); continue; }
    await page.waitForTimeout(900); // allow detail fetch
    // Capture rendered detail block
    const detail = await page.$eval('.task-detail', n => n.outerHTML).catch(() => null);
    if (!detail) { console.log('NO_DETAIL_BLOCK'); continue; }
    // Pull labeled rows + banners
    const parts = await page.evaluate(() => {
      const root = document.querySelector('.task-detail');
      if (!root) return null;
      return {
        title: root.querySelector('.task-detail-title')?.textContent || '',
        banners: Array.from(root.querySelectorAll('.error-banner')).map(b => b.textContent.trim()),
        rows: Array.from(root.querySelectorAll('.task-detail-row')).map(r => [
          r.querySelector('.task-detail-label')?.textContent || '',
          r.querySelector('.task-detail-value')?.textContent || '',
        ]),
        actions: Array.from(root.querySelectorAll('.task-action-button')).map(b => b.textContent.trim()),
        descCount: root.querySelectorAll('.task-detail-description').length,
      };
    });
    const api = await fetch(BASE + '/api/v1/tasks/' + t.key, { headers: { Authorization: 'Bearer ' + TOKEN }}).then(r=>r.json());
    console.log('RENDERED:', JSON.stringify(parts, null, 2));
    console.log('API_KEYS:', Object.keys(api).join(','));
    console.log('API_relevant: status=' + api.work_status + ' dwell=' + api.dwell_seconds + ' paused=' + api.project_paused + ' claim=' + api.claimed_by_session + ' assignee=' + api.assignee + ' priority=' + api.priority + ' bbb=' + JSON.stringify(api.relationships?.blocked_by || []) + ' labels=' + JSON.stringify(api.labels) + ' requires_human_review=' + api.requires_human_review + ' files=' + JSON.stringify(api.relevant_files));
    // Dwell ticking on terminal? Check that dwell row is absent for terminal
    const isTerminal = ['done', 'cancelled'].includes(api.work_status);
    const hasDwellRow = parts.rows.some(r => r[0] === 'Dwell');
    console.log('DWELL_GATE: isTerminal=' + isTerminal + ' hasDwellRow=' + hasDwellRow + ' -> ' + ((isTerminal && hasDwellRow) ? 'FAIL_DWELL_ON_TERMINAL' : 'OK'));
    await page.screenshot({ path: '/tmp/probe334_' + t.label + '.png', fullPage: false });
  }

  await browser.close();
}

run().catch(e => { console.error('FAIL', e); process.exit(1); });
