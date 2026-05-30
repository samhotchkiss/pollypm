// Definitive DOM inspection: are the AskUserQuestion menu options rendered as
// interactive elements (button/input/radio/[role]/onclick) or static text?
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

  // 1) Count interactive elements whose text relates to the menu options
  const interactive = await page.evaluate(() => {
    const sels = 'button, input, [role="button"], [role="radio"], [role="option"], [role="menuitem"], select, a[href], [onclick], [contenteditable="true"]';
    const els = [...document.querySelectorAll(sels)];
    const menuRe = /Bespoke SVG|Imagery medium|Hero treatment|Unsplash|book-stac|Submit/i;
    return els
      .filter(e => menuRe.test(e.innerText || e.value || e.getAttribute('aria-label') || ''))
      .map(e => ({
        tag: e.tagName.toLowerCase(),
        type: e.getAttribute('type') || null,
        role: e.getAttribute('role') || null,
        text: ((e.innerText || e.value || '').trim()).slice(0, 60),
      }));
  });
  console.log('INTERACTIVE_MENU_ELEMENTS:', JSON.stringify(interactive, null, 2));

  // 2) Inspect the DOM node that contains "Bespoke SVG illustration" — what tag, is it clickable?
  const nodeInfo = await page.evaluate(() => {
    function findContaining(text) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (node.nodeValue && node.nodeValue.includes(text)) return node.parentElement;
      }
      return null;
    }
    const el = findContaining('Bespoke SVG');
    if (!el) return { found: false };
    const chain = [];
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      chain.push({
        tag: cur.tagName.toLowerCase(),
        cls: (cur.className && cur.className.toString()).slice(0, 60),
        role: cur.getAttribute('role'),
        clickableTag: ['BUTTON','A','INPUT','SELECT'].includes(cur.tagName),
        hasOnclick: !!cur.onclick || cur.hasAttribute('onclick'),
        cursor: getComputedStyle(cur).cursor,
      });
      cur = cur.parentElement;
    }
    return { found: true, chain };
  });
  console.log('BESPOKE_NODE_CHAIN:', JSON.stringify(nodeInfo, null, 2));

  // 3) Is there any FORM with radios/checkboxes anywhere in the chat transcript?
  const formCounts = await page.evaluate(() => ({
    forms: document.querySelectorAll('form').length,
    radios: document.querySelectorAll('input[type="radio"]').length,
    checkboxes: document.querySelectorAll('input[type="checkbox"]').length,
    submitButtons: [...document.querySelectorAll('button,input[type="submit"]')].filter(b => /submit/i.test(b.innerText||b.value||'')).length,
    buttonsTotal: document.querySelectorAll('button').length,
  }));
  console.log('FORM_CONTROL_COUNTS:', JSON.stringify(formCounts));

  // 4) Try actually clicking the "Bespoke SVG" text to see if anything is bound
  const before = await page.evaluate(() => document.body.innerText.length);
  await page.getByText('Bespoke SVG illustration', { exact: false }).first().click({ timeout: 3000 }).catch(e => console.log('CLICK_BESPOKE_ERR:', e.message.split('\n')[0]));
  await page.waitForTimeout(1500);
  const after = await page.evaluate(() => document.body.innerText.length);
  console.log('CLICK_CHANGED_DOM_TEXT_LEN:', before, '->', after);

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
