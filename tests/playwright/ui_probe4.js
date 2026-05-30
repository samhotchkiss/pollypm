const { chromium } = require('@playwright/test');

const BASE = 'http://100.67.2.108:8765';
const UI = BASE + '/ui/';

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  // Measure response times via network events
  const timings = {};
  page.on('request', req => {
    if (req.url().includes('/api/')) timings[req.url()] = { start: Date.now(), method: req.method() };
  });
  page.on('response', resp => {
    if (timings[resp.url()]) {
      timings[resp.url()].elapsed = Date.now() - timings[resp.url()].start;
      timings[resp.url()].status = resp.status();
    }
  });

  await page.goto(UI, { waitUntil: 'load' });
  await page.waitForSelector('li', { timeout: 5000 }).catch(() => {});

  // Let page settle
  await page.waitForTimeout(500);

  // Click operator session (small text match to be precise)
  const t0 = Date.now();
  await page.locator('li').filter({ hasText: /^operator$/ }).click({ timeout: 5000 }).catch(async (e) => {
    console.log(`Exact operator click failed: ${e.message}, trying contains...`);
    await page.locator('li').filter({ hasText: 'operator' }).first().click({ timeout: 5000 }).catch(e2 => console.log(`Still failed: ${e2.message}`));
  });
  const clickElapsed = Date.now() - t0;
  console.log(`\nSession click elapsed: ${clickElapsed}ms`);
  
  // Wait for messages to load
  await page.waitForTimeout(12000);

  await page.screenshot({ path: '/tmp/ui_13_session_loaded.png', fullPage: false });
  const text = await page.evaluate(() => document.body.innerText);
  console.log(`\nPage text after messages load:\n${text.substring(0, 1000)}`);

  // Look for transcript content
  const hasMessages = text.includes('Polly') || text.length > 1500;
  const noSurface = text.includes('No surface selected');
  console.log(`Has messages: ${hasMessages}, No surface: ${noSurface}`);

  // Check for message input enabled
  const inputEnabled = await page.locator('input[type="text"], textarea').first().isEnabled().catch(() => false);
  console.log(`Input enabled: ${inputEnabled}`);

  // Now measure messages endpoint directly
  console.log('\n=== Messages endpoint timing (5 runs) ===');
  for (let i = 0; i < 5; i++) {
    const r = await page.evaluate(async (base) => {
      const t = Date.now();
      const r = await fetch(base + '/api/v1/chat/operator/messages?limit=50&direction=desc');
      const elapsed = Date.now() - t;
      const body = await r.json().catch(() => null);
      const msgCount = body?.messages?.length ?? 0;
      return { elapsed, status: r.status, msgCount };
    }, BASE);
    console.log(`  Run ${i+1}: ${r.elapsed}ms status=${r.status} msgs=${r.msgCount}`);
  }

  // Check dashboard timings captured from network
  console.log('\n=== Captured network timings ===');
  Object.entries(timings).forEach(([url, data]) => {
    const shortUrl = url.replace(BASE, '');
    console.log(`  ${data.method} ${shortUrl}: ${data.elapsed}ms (status ${data.status})`);
  });

  // Check for mobile: click a session on 360px
  await context.close();
  const mobileCtx = await browser.newContext({ viewport: { width: 360, height: 800 } });
  const mobilePg = await mobileCtx.newPage();
  await mobilePg.goto(UI, { waitUntil: 'load' });
  await mobilePg.waitForSelector('li', { timeout: 5000 }).catch(() => {});
  await mobilePg.screenshot({ path: '/tmp/ui_14_mobile_rail.png', fullPage: false });

  const mobileText = await mobilePg.evaluate(() => document.body.innerText);
  console.log(`\nMobile page text:\n${mobileText.substring(0, 400)}`);

  // Check for rail visibility on mobile
  const railVisible = await mobilePg.evaluate(() => {
    const lis = Array.from(document.querySelectorAll('li'));
    return lis.map(li => {
      const r = li.getBoundingClientRect();
      return { text: li.textContent?.trim().substring(0, 30), visible: r.width > 0 && r.height > 0, x: r.x, y: r.y, width: r.width };
    }).filter(x => x.text && x.visible).slice(0, 10);
  });
  console.log(`Mobile rail items: ${JSON.stringify(railVisible)}`);

  // Check Stop button — is it visible when nothing selected?
  const stopBtn = await mobilePg.locator('button:has-text("Stop")').count();
  const sendBtn = await mobilePg.locator('button:has-text("Send")').count();
  console.log(`Mobile Stop button count: ${stopBtn}, Send button: ${sendBtn}`);

  await mobileCtx.close();
  await browser.close();
}

run().catch(e => { console.error('PROBE4 ERROR:', e); process.exit(1); });
