// Confirm: enumerate ALL buttons + the single form. Verify none is a decision
// widget, and capture a zoomed screenshot of the menu region as static text.
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
  await page.goto('http://127.0.0.1:8765/ui/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(3500);
  const projList = page.locator('#project-list li, [data-project-key], .project-item');
  const n = await projList.count();
  for (let i = 0; i < n; i++) {
    const el = projList.nth(i);
    if (/savethenovel/i.test((await el.innerText().catch(() => '')) || '')) { await el.click().catch(() => {}); break; }
  }
  await page.waitForTimeout(2500);
  await page.getByText('architect_savethenovel', { exact: false }).first().click().catch(() => {});
  await page.waitForTimeout(4000);

  const allButtons = await page.locator('button').evaluateAll(bs => bs.map(b => (b.innerText||'').trim().replace(/\n/g,' ').slice(0,40)));
  console.log('ALL_BUTTONS:', JSON.stringify(allButtons));
  const formInfo = await page.locator('form').evaluateAll(fs => fs.map(f => ({
    inputs: [...f.querySelectorAll('input,textarea,select,button')].map(e => e.tagName.toLowerCase()+(e.type?':'+e.type:'')),
  })));
  console.log('FORM_INFO:', JSON.stringify(formInfo));

  // Zoom screenshot of the message that contains the menu line
  const menuMsg = page.locator('.message-text', { hasText: 'Imagery medium' }).first();
  if (await menuMsg.count()) {
    await menuMsg.scrollIntoViewIfNeeded().catch(()=>{});
    await page.waitForTimeout(500);
    await menuMsg.screenshot({ path: OUT + '/04-menu-line-static.png' }).catch(e => console.log('shot err', e.message));
  }
  // Full main pane screenshot showing the menu rendered as text
  await page.locator('main').first().screenshot({ path: OUT + '/05-main-pane.png' }).catch(()=>{});

  // The full menu option text block, for the report
  const menuBlock = await page.evaluate(() => {
    const t = document.body.innerText;
    const i = t.search(/What medium should the imagery/i);
    return i >= 0 ? t.slice(i, i + 900) : '(not found)';
  });
  console.log('MENU_BLOCK:\n' + menuBlock);

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
