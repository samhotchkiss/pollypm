// §3.1.3 + §3.2 focused probe
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8765';
const UI = BASE + '/ui/';

const results = {
  ttfp_ms: null,
  fully_painted_ms: null,
  network_idle_ms: null,
  first_click_latency_ms: null,
  warm_click_latency_ms: null,
  console_errors: [],
  request_failures: [],
  api_sessions_count: 0,
  api_tasks_count: 0,
  api_projects_count: 0,
  ui_chat_surfaces_count: 0,
  ui_task_surfaces_count: 0,
  ui_chat_surfaces: [],
  ui_task_surfaces: [],
  api_chat_surfaces: [],
  api_task_surfaces: [],
  api_projects: [],
  layout_shifts: [],
  empty_state_flash: false,
  findings: [],
};

async function run() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await ctx.newPage();

  page.on('console', (m) => {
    if (m.type() === 'error') results.console_errors.push(m.text().substring(0, 200));
  });
  page.on('requestfailed', (req) => {
    results.request_failures.push(`${req.method()} ${req.url()} :: ${req.failure()?.errorText}`);
  });

  // ---- §3.1.3: paint under realistic data ----
  console.log('\n=== §3.1.3: paint under realistic data ===');
  const t0 = Date.now();
  const respPromise = page.waitForResponse((r) => r.url().endsWith('/ui/') || r.url().endsWith('/ui'), { timeout: 10000 }).catch(() => null);
  await page.goto(UI, { waitUntil: 'commit' });
  // first contentful paint via Performance API
  await page.waitForLoadState('domcontentloaded');
  const ttfp = Date.now() - t0;
  results.ttfp_ms = ttfp;
  console.log(`DOMContentLoaded: ${ttfp}ms`);

  // detect empty-state flash: check at 200ms if there's "no surfaces registered" text before data loads
  await page.waitForTimeout(200);
  const earlyText = await page.evaluate(() => document.body.innerText || '');
  if (/no surfaces registered|no projects/.test(earlyText)) {
    results.empty_state_flash = true;
    console.log('FLASH OF EMPTY STATE detected at t+200ms');
  }

  // Wait for surface list to actually have items (not just "loading...")
  let fullyPainted = null;
  const paintT0 = Date.now();
  try {
    await page.waitForFunction(() => {
      const list = document.getElementById('surface-list');
      if (!list) return false;
      const items = list.querySelectorAll('li');
      if (items.length === 0) return false;
      const texts = Array.from(items).map((li) => li.textContent || '');
      // require at least one item that is NOT loading/empty
      return texts.some((t) => !/^loading|^no surfaces|^error/i.test(t.trim()));
    }, null, { timeout: 15000 });
    fullyPainted = Date.now() - t0;
    results.fully_painted_ms = fullyPainted;
    console.log(`Surface list populated: ${fullyPainted}ms`);
  } catch (e) {
    console.log(`TIMEOUT waiting for surface list to populate: ${e.message.substring(0, 100)}`);
    results.findings.push({ type: 'bug', severity: 'high', title: '§3.1.3: surface list never populates within 15s', detail: e.message });
  }

  await page.waitForLoadState('networkidle').catch(() => {});
  results.network_idle_ms = Date.now() - t0;
  console.log(`Network idle: ${results.network_idle_ms}ms`);

  await page.screenshot({ path: '/tmp/probe_3_painted.png', fullPage: false }).catch(() => {});

  // Measure CLS-ish: capture bounding boxes of key panes before/after second poll cycle
  const layoutBefore = await page.evaluate(() => {
    const ids = ['surface-list', 'message-list', 'inbox-list', 'pane-title'];
    const out = {};
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) {
        const r = el.getBoundingClientRect();
        out[id] = { x: r.x, y: r.y, w: r.width, h: r.height };
      }
    }
    return out;
  });

  // First (cold) click — pick first chat surface in the rail
  console.log('\n--- Cold click on first surface ---');
  const firstSurfaceSel = '#surface-list li[data-session]';
  await page.waitForSelector(firstSurfaceSel, { timeout: 5000 }).catch(() => null);
  const firstName = await page.locator(firstSurfaceSel).first().getAttribute('data-session').catch(() => null);
  console.log(`First surface in rail: ${firstName}`);
  if (firstName) {
    const c0 = Date.now();
    await page.locator(firstSurfaceSel).first().click({ timeout: 5000 }).catch((e) => {
      console.log(`Cold click failed: ${e.message.substring(0, 100)}`);
    });
    // wait for pane-title to change OR for message-list to update
    await page.waitForFunction((name) => {
      const t = document.getElementById('pane-title');
      return t && t.textContent && t.textContent.trim() === name;
    }, firstName, { timeout: 5000 }).catch(() => {});
    results.first_click_latency_ms = Date.now() - c0;
    console.log(`Cold click latency: ${results.first_click_latency_ms}ms`);
    if (results.first_click_latency_ms > 1000) {
      results.findings.push({
        type: 'perf',
        severity: 'ship-blocker',
        title: `§3.1.3: cold surface click ${results.first_click_latency_ms}ms (>1s budget)`,
        detail: `Clicking ${firstName} took ${results.first_click_latency_ms}ms — breaches the 1-second click rule.`,
      });
    }
  }

  // Warm click — re-select same surface
  if (firstName) {
    const c1 = Date.now();
    await page.locator(firstSurfaceSel).first().click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(50);
    results.warm_click_latency_ms = Date.now() - c1;
    console.log(`Warm click latency: ${results.warm_click_latency_ms}ms`);
  }

  const layoutAfter = await page.evaluate(() => {
    const ids = ['surface-list', 'message-list', 'inbox-list', 'pane-title'];
    const out = {};
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) {
        const r = el.getBoundingClientRect();
        out[id] = { x: r.x, y: r.y, w: r.width, h: r.height };
      }
    }
    return out;
  });

  // Detect significant layout shift in key panes
  for (const id of Object.keys(layoutBefore)) {
    const a = layoutBefore[id]; const b = layoutAfter[id];
    if (!a || !b) continue;
    if (Math.abs(a.x - b.x) > 4 || Math.abs(a.y - b.y) > 4) {
      results.layout_shifts.push({ id, before: a, after: b });
    }
  }

  // ---- §3.2: surface enumeration parity ----
  console.log('\n=== §3.2: surface enumeration parity ===');

  // Capture API canonical surfaces from the live page (uses same origin auth)
  const apiData = await page.evaluate(async (base) => {
    async function fetchJson(p) {
      const r = await fetch(base + p);
      if (!r.ok) return { error: r.status, text: await r.text() };
      return await r.json();
    }
    const [sessions, projects, tasks] = await Promise.all([
      fetchJson('/api/v1/chat/sessions'),
      fetchJson('/api/v1/projects'),
      fetchJson('/api/v1/tasks?limit=200'),
    ]);
    return { sessions, projects, tasks };
  }, BASE);

  if (apiData.sessions && Array.isArray(apiData.sessions.sessions)) {
    results.api_chat_surfaces = apiData.sessions.sessions.map((s) => s.session_name).sort();
    results.api_sessions_count = results.api_chat_surfaces.length;
  }
  if (apiData.projects && Array.isArray(apiData.projects.items)) {
    results.api_projects = apiData.projects.items.map((p) => p.key).sort();
    results.api_projects_count = results.api_projects.length;
  }
  if (apiData.tasks) {
    const list = apiData.tasks.items || [];
    results.api_task_surfaces_raw = list.map((t) => `${t.project}/${t.task_number}`).sort();
    results.api_task_count_raw = results.api_task_surfaces_raw.length;
    // Replicate the UI's dedupe key
    const seen = new Set();
    const dedup = [];
    for (const t of list) {
      const k = `${t.project} ${t.work_status} ${(t.title || '').trim().toLowerCase()}`;
      if (seen.has(k)) continue;
      seen.add(k);
      dedup.push(`${t.project}/${t.task_number}`);
    }
    results.api_task_surfaces = dedup.sort();
    results.api_tasks_count = dedup.length;
    results.api_task_warnings = apiData.tasks.warnings || [];
  }
  console.log(`API: ${results.api_sessions_count} chat sessions, ${results.api_tasks_count} tasks, ${results.api_projects_count} projects`);

  // Walk the rendered UI surface list — selectors are data-session / data-task
  const railUI = await page.evaluate(() => {
    const list = document.getElementById('surface-list');
    if (!list) return { error: 'no #surface-list' };
    const chat = Array.from(list.querySelectorAll('li[data-session]'))
      .map((li) => li.getAttribute('data-session'));
    const tasks = Array.from(list.querySelectorAll('li[data-task]'))
      .map((li) => li.getAttribute('data-task'));
    const allItems = list.querySelectorAll('li');
    return { chat, tasks, raw_count: allItems.length };
  });
  results.ui_chat_surfaces = (railUI.chat || []).sort();
  results.ui_task_surfaces = (railUI.tasks || []).sort();
  results.ui_chat_surfaces_count = results.ui_chat_surfaces.length;
  results.ui_task_surfaces_count = results.ui_task_surfaces.length;
  console.log(`UI rail: ${results.ui_chat_surfaces_count} chat surfaces, ${results.ui_task_surfaces_count} task surfaces (raw_items=${railUI.raw_count})`);

  // Parity diff
  function diff(a, b) {
    const sa = new Set(a); const sb = new Set(b);
    return {
      only_in_a: [...sa].filter((x) => !sb.has(x)),
      only_in_b: [...sb].filter((x) => !sa.has(x)),
    };
  }
  const chatDiff = diff(results.api_chat_surfaces, results.ui_chat_surfaces);
  const taskDiff = diff(results.api_task_surfaces, results.ui_task_surfaces);

  results.chat_diff = chatDiff;
  results.task_diff = taskDiff;
  console.log(`\nChat surface diff:`);
  console.log(`  API only: ${JSON.stringify(chatDiff.only_in_a).substring(0, 400)}`);
  console.log(`  UI only:  ${JSON.stringify(chatDiff.only_in_b).substring(0, 400)}`);
  console.log(`\nTask surface diff:`);
  console.log(`  API only: ${JSON.stringify(taskDiff.only_in_a).substring(0, 400)}`);
  console.log(`  UI only:  ${JSON.stringify(taskDiff.only_in_b).substring(0, 400)}`);

  if (chatDiff.only_in_a.length > 0) {
    results.findings.push({
      type: 'bug',
      severity: 'parity',
      title: `§3.2: ${chatDiff.only_in_a.length} chat surface(s) in API but missing from UI rail`,
      detail: `Missing in rail: ${chatDiff.only_in_a.slice(0, 20).join(', ')}`,
    });
  }
  if (chatDiff.only_in_b.length > 0) {
    results.findings.push({
      type: 'bug',
      severity: 'parity',
      title: `§3.2: ${chatDiff.only_in_b.length} chat surface(s) in UI rail but not returned by API`,
      detail: `Extra in rail: ${chatDiff.only_in_b.slice(0, 20).join(', ')}`,
    });
  }
  if (taskDiff.only_in_a.length > 0) {
    results.findings.push({
      type: 'bug',
      severity: 'parity',
      title: `§3.2: ${taskDiff.only_in_a.length} task surface(s) in API but missing from UI rail`,
      detail: `Missing in rail: ${taskDiff.only_in_a.slice(0, 20).join(', ')}`,
    });
  }

  // Check ordering: UI order should be the same as API insertion order, grouped
  const apiOrder = (apiData.sessions && apiData.sessions.sessions || []).map((s) => s.session_name);
  const railOrderUI = await page.evaluate(() => {
    const list = document.getElementById('surface-list');
    if (!list) return [];
    return Array.from(list.querySelectorAll('li[data-session]')).map((li) => li.getAttribute('data-session'));
  });
  // Detect re-ordering
  const apiOrderFiltered = apiOrder.filter((n) => railOrderUI.includes(n));
  let firstOrderMismatch = -1;
  for (let i = 0; i < Math.min(apiOrderFiltered.length, railOrderUI.length); i++) {
    if (apiOrderFiltered[i] !== railOrderUI[i]) { firstOrderMismatch = i; break; }
  }
  results.ui_chat_order = railOrderUI;
  results.api_chat_order = apiOrder;
  if (firstOrderMismatch >= 0) {
    results.findings.push({
      type: 'bug',
      severity: 'parity',
      title: `§3.2: chat surface ordering diverges from API at index ${firstOrderMismatch}`,
      detail: `API[${firstOrderMismatch}]=${apiOrderFiltered[firstOrderMismatch]}, UI[${firstOrderMismatch}]=${railOrderUI[firstOrderMismatch]}`,
    });
  }
  results.first_order_mismatch_idx = firstOrderMismatch;

  // Check for duplicates
  const dupChat = results.ui_chat_surfaces.filter((v, i, a) => a.indexOf(v) !== i);
  if (dupChat.length > 0) {
    results.findings.push({
      type: 'bug', severity: 'parity',
      title: `§3.2: duplicate chat surfaces in UI rail: ${dupChat.length}`,
      detail: `Duplicates: ${dupChat.slice(0, 10).join(', ')}`,
    });
  }

  // Three-way parity stub: read /tmp/cli-sessions.txt if present
  try {
    const cli = fs.readFileSync('/tmp/cli-sessions.txt', 'utf8').split('\n').filter((l) => l && !l.startsWith('TOTAL'));
    results.cli_session_count = cli.length;
    const cliSorted = [...cli].sort();
    const apiVsCli = diff(results.api_chat_surfaces, cliSorted);
    results.api_vs_cli_diff = apiVsCli;
    console.log(`\nThree-way (API vs pm sessions --health):`);
    console.log(`  In API only: ${apiVsCli.only_in_a.length} (e.g. ${apiVsCli.only_in_a.slice(0,5).join(', ')})`);
    console.log(`  In CLI only: ${apiVsCli.only_in_b.length} (e.g. ${apiVsCli.only_in_b.slice(0,5).join(', ')})`);
  } catch (e) {
    console.log(`No /tmp/cli-sessions.txt: ${e.message}`);
  }

  await browser.close();

  fs.writeFileSync('/tmp/probe_3_1_3_results.json', JSON.stringify(results, null, 2));
  console.log('\nWrote /tmp/probe_3_1_3_results.json');
  console.log(`Findings: ${results.findings.length}`);
  for (const f of results.findings) console.log(`  - [${f.severity}] ${f.title}`);
}

run().catch((e) => { console.error('PROBE ERROR:', e); process.exit(1); });
