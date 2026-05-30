const { chromium } = require('@playwright/test');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  // Track XHR/fetch to find the actual messages API path
  const networkCalls = [];
  page.on('request', req => {
    if (req.url().includes('/api/')) {
      networkCalls.push({ method: req.method(), url: req.url() });
    }
  });
  page.on('response', resp => {
    if (resp.url().includes('/api/')) {
      const entry = networkCalls.find(c => c.url === resp.url() && !c.status);
      if (entry) entry.status = resp.status();
    }
  });

  await page.goto(UI, { waitUntil: 'load' });
  await page.waitForSelector('li', { timeout: 5000 }).catch(() => {});

  console.log('Initial network calls:');
  networkCalls.forEach(c => console.log(`  ${c.method} ${c.url} -> ${c.status}`));

  // Click first session item - clear existing network calls first
  networkCalls.length = 0;

  const t0 = Date.now();
  // The first LI that's not the header
  const sessionItems = await page.locator('li:not(.surface-group)').all();
  console.log(`\nSession items in list: ${sessionItems.length}`);
  
  if (sessionItems.length > 0) {
    await sessionItems[0].click({ timeout: 5000 }).catch(e => console.log(`Click error: ${e.message}`));
    const elapsed = Date.now() - t0;
    console.log(`Click first session (non-header): ${elapsed}ms`);
    
    // Wait for network to settle
    await page.waitForTimeout(4000);
    
    console.log('\nNetwork calls after session click:');
    networkCalls.forEach(c => console.log(`  ${c.method} ${c.url} -> ${c.status}`));
    
    await page.screenshot({ path: '/tmp/ui_11_session_active.png', fullPage: false });
    
    const pageText = await page.evaluate(() => document.body.innerText);
    console.log(`\nPage text after click:\n${pageText.substring(0, 800)}`);
    
    // Check input state
    const inputVisible = await page.locator('input[type="text"], textarea').first().isVisible().catch(() => false);
    const inputEnabled = await page.locator('input[type="text"], textarea').first().isEnabled().catch(() => false);
    console.log(`\nInput visible: ${inputVisible}, enabled: ${inputEnabled}`);
    
    // Try to get input value/placeholder
    const inputPlaceholder = await page.locator('input[type="text"], textarea').first().getAttribute('placeholder').catch(() => null);
    console.log(`Input placeholder: ${inputPlaceholder}`);
    
    // Check for "No surface selected" vs actual session
    const noSurface = pageText.includes('No surface selected');
    const hasTranscript = pageText.includes('operator') && pageText.length > 500;
    console.log(`"No surface selected" visible: ${noSurface}`);
    console.log(`Appears to have session loaded: ${hasTranscript}`);
  }

  // Now find a clickable session that actually works — try by clicking a session name 
  networkCalls.length = 0;
  const t1 = Date.now();
  
  // Try clicking by text
  await page.click('li:has-text("operator")', { timeout: 5000 }).catch(e => console.log(`operator click error: ${e.message}`));
  const elapsed2 = Date.now() - t1;
  console.log(`\nClick 'operator' session: ${elapsed2}ms`);
  await page.waitForTimeout(3000);
  
  console.log('\nNetwork calls after operator session click:');
  networkCalls.forEach(c => console.log(`  ${c.method} ${c.url} -> ${c.status}`));
  
  await page.screenshot({ path: '/tmp/ui_12_operator_session.png', fullPage: false });
  const textAfterOp = await page.evaluate(() => document.body.innerText);
  console.log(`\nPage text after operator click:\n${textAfterOp.substring(0, 800)}`);

  // Test sessions/{name}/messages pattern vs sessions/{id}/messages
  const testPaths = [
    '/api/v1/sessions/operator/messages',
    '/api/v1/chat/operator/messages',
    '/api/v1/sessions/operator_polly/messages'
  ];
  for (const p of testPaths) {
    const r = await page.evaluate(async (args) => {
      const { base, path } = args;
      const t = Date.now();
      const resp = await fetch(base + path);
      const elapsed = Date.now() - t;
      const body = await resp.json().catch(() => null);
      return { path, status: resp.status, elapsed, body: JSON.stringify(body).substring(0, 150) };
    }, { base: BASE, path: p });
    console.log(`${p}: status=${r.status} ${r.elapsed}ms | ${r.body}`);
  }

  await browser.close();
}

run().catch(e => { console.error('PROBE3 ERROR:', e); process.exit(1); });
