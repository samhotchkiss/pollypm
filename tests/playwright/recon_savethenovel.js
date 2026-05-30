// Recon: navigate to savethenovel -> Sage chat, dump structure to determine
// whether the AskUserQuestion decision menu renders CLICKABLE options.
const { chromium } = require('@playwright/test');
const fs = require('fs');
const TOKEN = fs.readFileSync(process.env.HOME + '/.pollypm/api-token', 'utf8').trim();
const OUT = '/tmp/cockpit-web3';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    extraHTTPHeaders: { Authorization: 'Bearer ' + TOKEN },
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });

  await page.goto('http://127.0.0.1:8765/ui/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(4000);
  await page.screenshot({ path: OUT + '/00-landing.png', fullPage: false });

  // Find and click savethenovel in the project list
  const projList = page.locator('#project-list li, [data-project-key], .project-item');
  const n = await projList.count();
  console.log('PROJECT_LIST_ITEMS:', n);
  let clicked = false;
  for (let i = 0; i < n; i++) {
    const el = projList.nth(i);
    const txt = (await el.innerText().catch(() => '')) || '';
    const key = (await el.getAttribute('data-project-key').catch(() => '')) || '';
    if (/savethenovel/i.test(txt) || /savethenovel/i.test(key)) {
      await el.click().catch(e => console.log('click err', e.message));
      clicked = true;
      console.log('CLICKED_PROJECT idx=' + i + ' text=' + JSON.stringify(txt.slice(0,40)) + ' key=' + key);
      break;
    }
  }
  console.log('SAVETHENOVEL_CLICKED:', clicked);
  await page.waitForTimeout(3500);
  await page.screenshot({ path: OUT + '/01-project-selected.png', fullPage: false });

  // Dump any tab/nav labels visible (to find the Chat surface)
  const navTexts = await page.locator('button, a, [role="tab"], .tab, .nav-item').evaluateAll(
    els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 40)
  );
  const uniq = [...new Set(navTexts)];
  console.log('VISIBLE_BUTTONS_TABS:', JSON.stringify(uniq.slice(0, 60)));

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
