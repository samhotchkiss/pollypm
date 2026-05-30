// Live probe: does the Activity panel render real data, and how long does
// a project click take to settle? Runs against the REAL daily-driver server.
const { chromium } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const slow = [];
  page.on('requestfinished', async (req) => {
    const t = req.timing();
    if (t && /\/api\/v1\/(audit|tasks|dashboard|chat|inbox)/.test(req.url())) {
      const dur = t.responseEnd;
      if (dur > 800) slow.push(req.url().replace(/.*\/api\/v1/, '') + '  ' + Math.round(dur) + 'ms');
    }
  });
  await page.goto('http://127.0.0.1:8765/ui/', { waitUntil: 'domcontentloaded', timeout: 25000 });
  // wait for activity panel to settle out of loading state
  await page.waitForTimeout(8000);
  const feedText = (await page.locator('#activity-feed').innerText().catch(() => '')).slice(0, 300);
  const summaryText = (await page.locator('#activity-summary').innerText().catch(() => '')).replace(/\n/g, ' ').slice(0, 200);
  const hasError = await page.locator('#activity-feed .fetch-retry').count();
  console.log('ACTIVITY_HAS_RETRY_BUTTON:', hasError);
  console.log('ACTIVITY_SUMMARY:', JSON.stringify(summaryText));
  console.log('ACTIVITY_FEED_TOP:', JSON.stringify(feedText));

  // click first project, measure time until activity panel re-settles
  const firstProj = page.locator('#project-list li[role], #project-list li').first();
  const before = Date.now();
  await firstProj.click().catch(() => {});
  // poll until activity-feed leaves "loading activity..." state
  let settled = before;
  for (let i = 0; i < 80; i++) {
    const txt = await page.locator('#activity-feed').innerText().catch(() => '');
    if (!/loading activity/i.test(txt)) { settled = Date.now(); break; }
    await page.waitForTimeout(100);
  }
  console.log('PROJECT_CLICK_ACTIVITY_SETTLE_MS:', settled - before);
  const postFeed = (await page.locator('#activity-feed').innerText().catch(() => '')).slice(0, 200);
  console.log('POST_CLICK_FEED_TOP:', JSON.stringify(postFeed));
  console.log('SLOW_API_CALLS(>800ms):');
  for (const s of slow) console.log('  ' + s);
  const errs = await page.evaluate(() => window.__probeConsoleErrors || []);
  console.log('CONSOLE_ERRORS:', errs.length);
  await browser.close();
})();
