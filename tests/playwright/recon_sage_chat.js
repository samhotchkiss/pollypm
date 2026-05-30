// Open architect_savethenovel (Sage) chat surface and dump the decision-menu
// structure: are AskUserQuestion options CLICKABLE elements or static text?
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
  page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE_ERR:', m.text()); });

  await page.goto('http://127.0.0.1:8765/ui/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(3500);

  // select savethenovel project
  const projList = page.locator('#project-list li, [data-project-key], .project-item');
  const n = await projList.count();
  for (let i = 0; i < n; i++) {
    const el = projList.nth(i);
    const txt = (await el.innerText().catch(() => '')) || '';
    if (/savethenovel/i.test(txt)) { await el.click().catch(() => {}); break; }
  }
  await page.waitForTimeout(2500);

  // Click the architect_savethenovel / Sage chat surface (bottom-left chat list)
  const sage = page.getByText('architect_savethenovel', { exact: false }).first();
  const sageCount = await sage.count();
  console.log('SAGE_SURFACE_FOUND:', sageCount);
  if (sageCount) {
    await sage.click().catch(e => console.log('sage click err', e.message));
  }
  await page.waitForTimeout(4000);
  await page.screenshot({ path: OUT + '/02-sage-chat.png', fullPage: false });

  // Dump the main pane header + scroll the chat area to bottom to load menu
  const header = await page.locator('h1, h2, .surface-title, .chat-header').allInnerTexts().catch(() => []);
  console.log('HEADERS:', JSON.stringify(header.slice(0, 8)));

  // Try to scroll chat transcript to bottom
  await page.evaluate(() => {
    const cands = document.querySelectorAll('[class*="transcript"],[class*="messages"],[class*="chat"],[id*="chat"],main,.feed');
    cands.forEach(c => { try { c.scrollTop = c.scrollHeight; } catch (e) {} });
  });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: OUT + '/03-sage-chat-bottom.png', fullPage: false });

  // Search the whole document text for menu signals
  const bodyText = await page.evaluate(() => document.body.innerText);
  const hasMenuText = /Imagery medium|Hero treatment|Bespoke SVG|Submit/i.test(bodyText);
  console.log('MENU_TEXT_PRESENT(anywhere):', hasMenuText);
  // print the chunk around "Imagery" if present
  const idx = bodyText.search(/Imagery medium/i);
  if (idx >= 0) console.log('MENU_CONTEXT:', JSON.stringify(bodyText.slice(Math.max(0,idx-200), idx+600)));
  else console.log('MAIN_PANE_TEXT_SAMPLE:', JSON.stringify(bodyText.slice(0, 1200)));

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
