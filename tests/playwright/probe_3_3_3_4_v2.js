// §3.3 + §3.4 probe v2 — drive selectTask for any task by injecting into taskSurfaces
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8765';
const UI = BASE + '/ui/';
const TOKEN = 'ysQmcaWCv7wWi3vUkz4jqcNKSItsxvN2ARRs88jNvDQ';

async function api(path) {
  const r = await fetch(BASE + path, { headers: { Authorization: 'Bearer ' + TOKEN }});
  return r.json();
}

function expectedFields(api) {
  // Fields the test plan demands be representable in UI detail panel
  return {
    title: api.title,
    status: api.work_status,
    dwell: api.dwell_seconds,
    blocked_by: api.relationships?.blocked_by || [],
    project_paused: api.project_paused,
    claimed_by_session: api.claimed_by_session,
    priority: api.priority,
    assignee: api.assignee,
    type: api.type,
    description: api.description,
    acceptance_criteria: api.acceptance_criteria,
    constraints: api.constraints,
    labels: api.labels || [],
    relevant_files: api.relevant_files || [],
    requires_human_review: api.requires_human_review,
    plan_version: api.plan_version,
    state_entered_at: api.state_entered_at,
    created_at: api.created_at,
    updated_at: api.updated_at,
    age_seconds: api.age_seconds,
  };
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

  // Probe targets: cover each state. For ones not in rail, force inject.
  const targets = [
    'russell/376',                  // draft (rail)
    'smoketest/49',                 // queued (rail)
    'russell/374',                  // cancelled (rail)
    'samblog/4',                    // blocked (NOT in rail, inject)
    'savethenovel/94',              // on_hold (NOT in rail, inject)
    'pollypm/221',                  // done (NOT in rail, inject)
    'pr2316_drift_1779720195/1',    // paused project (verify banner)
  ];

  const findings = [];

  for (const key of targets) {
    console.log('\n=== ' + key + ' ===');
    const apiResp = await api('/api/v1/tasks/' + key);
    if (apiResp.error) { console.log('API_ERR:', apiResp.error); continue; }

    // Inject the task into taskSurfaces so selectTask succeeds
    await page.evaluate((t) => {
      const surf = {
        key: t.task_id,
        task_id: t.task_id,
        project: t.project,
        task_number: t.task_number,
        title: t.title,
        work_status: t.work_status,
        type: t.type,
        priority: t.priority,
        assignee: t.assignee,
        claimed_by_session: t.claimed_by_session,
        dwell_seconds: t.dwell_seconds,
        project_paused: t.project_paused,
      };
      const arr = window.PollyPM.state.taskSurfaces;
      // Remove any existing entry with this key, push new
      const idx = arr.findIndex(x => x.key === surf.key);
      if (idx >= 0) arr.splice(idx, 1);
      arr.push(surf);
      window.PollyPM.selectTask(surf.key);
    }, apiResp);
    await page.waitForTimeout(1200); // fetch detail

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
        descriptionTexts: Array.from(root.querySelectorAll('.task-detail-description')).map(d => d.textContent.slice(0, 80)),
        actions: Array.from(root.querySelectorAll('.task-action-button')).map(b => b.textContent.trim()),
        bodyText: root.textContent,
      };
    });

    if (!parts) { console.log('NO_DETAIL'); continue; }

    const exp = expectedFields(apiResp);
    const isTerminal = ['done', 'cancelled'].includes(apiResp.work_status);
    const hasDwellRow = parts.rows.some(r => r[0] === 'Dwell');
    const rowLabels = parts.rows.map(r => r[0]);
    const banners = parts.banners.join(' | ');

    // §3.3 checks
    if (isTerminal && hasDwellRow) {
      findings.push(`§3.3:${key}: dwell row rendered on terminal-state task (expected hidden post-#2355)`);
    }
    if (exp.project_paused === true && !banners.includes('PROJECT PAUSED')) {
      findings.push(`§3.3:${key}: project_paused=true but paused-project banner NOT rendered`);
    }
    if (exp.claimed_by_session && !isTerminal && exp.dwell && exp.dwell > 300 && !banners.includes('claimed-by')) {
      findings.push(`§3.3:${key}: dead-claim warning expected but not rendered`);
    }

    // §3.4 checks — field coverage
    const haveText = (v) => v && parts.bodyText.includes(String(v));
    if (exp.title && !haveText(exp.title)) findings.push(`§3.4:${key}: title missing from panel`);
    if (!rowLabels.includes('Status')) findings.push(`§3.4:${key}: Status row missing`);
    if (exp.priority && !rowLabels.includes('Priority')) findings.push(`§3.4:${key}: Priority row missing`);
    if (exp.assignee && !rowLabels.includes('Assignee')) findings.push(`§3.4:${key}: Assignee row missing (API has assignee)`);
    if (exp.blocked_by.length > 0 && !rowLabels.includes('Blocked by')) findings.push(`§3.4:${key}: Blocked by row missing (API has ${exp.blocked_by.length})`);
    // Fields that are silently dropped from the UI even though they exist in API
    if (exp.labels.length > 0 && !haveText(exp.labels[0])) findings.push(`§3.4:${key}: labels not represented in UI (API has ${JSON.stringify(exp.labels)})`);
    if (exp.relevant_files.length > 0 && !haveText(exp.relevant_files[0])) findings.push(`§3.4:${key}: relevant_files not represented in UI (API has ${exp.relevant_files.length})`);
    if (exp.constraints && !haveText(exp.constraints.slice(0, 30))) findings.push(`§3.4:${key}: constraints not represented in UI (API has them)`);
    if (exp.type && exp.type !== 'task' && !haveText(exp.type)) findings.push(`§3.4:${key}: type='${exp.type}' not represented`);
    if (exp.requires_human_review && !haveText('review')) findings.push(`§3.4:${key}: requires_human_review=true not represented`);

    console.log('STATUS:', apiResp.work_status, '| dwell_row:', hasDwellRow, '| banners:', banners || '(none)');
    console.log('ROWS:', rowLabels.join(','));
    console.log('ACTIONS:', parts.actions.join(','));
    console.log('API_HAS: labels=' + exp.labels.length + ' files=' + exp.relevant_files.length + ' constraints=' + (!!exp.constraints) + ' blocked_by=' + exp.blocked_by.length + ' paused=' + exp.project_paused + ' claim=' + exp.claimed_by_session);
  }

  console.log('\n=== FINDINGS ===');
  for (const f of findings) console.log('- ' + f);
  console.log('TOTAL FINDINGS:', findings.length);

  await browser.close();
}

run().catch(e => { console.error('FAIL', e); process.exit(1); });
