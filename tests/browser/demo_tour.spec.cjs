// Real DOM regressions; run against a separately started local offline demo.
// Requires Playwright and Chrome already installed; no candidate execution.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const base = process.env.TRACEJUDGE_DEMO_URL || 'http://127.0.0.1:8770';
assert.equal(new URL(base).hostname, '127.0.0.1');
let browser;
before(async () => {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.TRACEJUDGE_CHROME_PATH ? { executablePath: process.env.TRACEJUDGE_CHROME_PATH } : {}),
  });
});
after(async () => { await browser?.close(); });

async function pageFor(t, viewport = { width: 1440, height: 1000 }) {
  const context = await browser.newContext({ viewport });
  const errors = [], forbidden = [];
  await context.route('**/*', route => {
    const request = route.request();
    if (request.method() !== 'GET' || new URL(request.url()).origin !== new URL(base).origin) {
      forbidden.push(request.method() + ' ' + request.url());
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  t.after(async () => {
    await context.close();
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
  });
  return page;
}

async function ready(page, index) {
  await page.waitForFunction(i => {
    const bar = document.querySelector('#tour-progress');
    const status = document.querySelector('#tour-status').textContent;
    return bar.textContent.startsWith(i + ' /') && (status.includes('已定位') || status.includes('仍可前进'));
  }, index);
}

test('exit restores selected cases, expanded details and exact scroll without a reload', async t => {
  const page = await pageFor(t);
  await page.goto(base + '/?case=find_char_long');
  await page.locator('#featured-detail').waitFor({ state: 'visible' });
  await page.locator('[data-case-id="equivalent_implementation"]').dispatchEvent('click');
  await page.locator('#featured-detail summary').first().dispatchEvent('click');
  await page.evaluate(() => scrollTo({ top: 550, behavior: 'instant' }));
  const originalScroll = await page.evaluate(() => scrollY);
  const originalURL = page.url();
  await page.locator('#open-tour').dispatchEvent('click');
  await ready(page, 1);
  await page.locator('#tour-next').click(); await ready(page, 2);
  await page.locator('#tour-next').click(); await ready(page, 3);
  await page.locator('#tour-exit').click();
  await page.waitForFunction(y => Math.abs(scrollY - y) < 2 &&
    document.querySelector('#featured-case-title').textContent.startsWith('find_char_long'), originalScroll);
  await page.waitForTimeout(400); // catches a late smooth-scroll or focus jump
  assert.equal(page.url(), originalURL);
  assert.ok(Math.abs((await page.evaluate(() => scrollY)) - originalScroll) < 2);
  assert.equal(await page.locator('[data-case-id="equivalent_implementation"]').getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('#featured-detail details').first().evaluate(node => node.open), true);
});

test('recording mode restores the scene and scroll after all four stops', async t => {
  const page = await pageFor(t);
  await page.goto(base + '/?recording=1');
  await page.locator('.scene-tab').nth(7).click();
  await page.evaluate(() => scrollTo({ top: 350, behavior: 'instant' }));
  const originalScroll = await page.evaluate(() => scrollY);
  await page.locator('#open-tour').dispatchEvent('click');
  for (let i = 1; i <= 4; i++) {
    await ready(page, i);
    assert.equal(await page.locator('#presenter').isVisible(), false);
    if (i < 4) await page.locator('#tour-next').click();
  }
  await page.keyboard.press('Escape');
  await page.waitForFunction(y => Math.abs(scrollY - y) < 2, originalScroll);
  assert.match(await page.locator('#scene-count').innerText(), /^08/);
  assert.equal(await page.locator('#presenter').isVisible(), true);
  assert.equal(await page.locator('.tour-target').count(), 0);
});

test('third stop uses the verified replay metric and receipt identity', async t => {
  const page = await pageFor(t);
  await page.goto(base + '/?tour=1&case=find_char_long');
  await ready(page, 1);
  assert.match(await page.locator('#featured-case-title').innerText(), /^tuple_str_int/);
  await page.locator('#tour-next').click(); await ready(page, 2);
  await page.locator('#tour-next').click(); await ready(page, 3);
  assert.equal(await page.locator('.tour-target').getAttribute('data-regression-metric'), 'counterexample_replay_pass');
  assert.match(await page.locator('#tour-status').innerText(), /phase4_public_replay_receipt_v1/);
});

for (const failure of ['503', 'missing_receipt', 'wrong_source', 'not_reproduced']) {
  test('third stop reports ' + failure + ' without claiming successful replay', async t => {
    const page = await pageFor(t);
    await page.route('**/api/regression', async route => {
      if (failure === '503') return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
      const response = await route.fetch();
      const data = await response.json();
      if (failure === 'missing_receipt') delete data.source.replay_receipt_id;
      if (failure === 'wrong_source') data.source.public_counterfactual_sha256 = 'wrong';
      if (failure === 'not_reproduced') data.metrics.counterexample_replay_pass.numerator = 0;
      await route.fulfill({ response, json: data });
    });
    await page.goto(base + '/?tour=1'); await ready(page, 1);
    await page.locator('#tour-next').click(); await ready(page, 2);
    await page.locator('#tour-next').click(); await ready(page, 3);
    assert.match(await page.locator('#tour-title').innerText(), failure === 'not_reproduced' ? /未确认失败复现/ : /暂不可用/);
    if (failure !== 'not_reproduced') assert.doesNotMatch(await page.locator('#tour-status').innerText(), /已定位/);
    await page.locator('#tour-next').click(); await ready(page, 4);
    await page.locator('#tour-exit').click();
    assert.equal(await page.locator('#tour-bar').isVisible(), false);
  });
}

test('a delayed case response cannot reactivate a tour after exit', async t => {
  const page = await pageFor(t);
  let release, arrived;
  const gate = new Promise(resolve => { release = resolve; });
  const requested = new Promise(resolve => { arrived = resolve; });
  await page.route('**/api/featured-process-cases', async route => {
    arrived(); await gate; await route.continue();
  });
  await page.goto(base + '/');
  await page.locator('#open-tour').click(); await requested;
  await page.locator('#tour-exit').click(); release();
  await page.waitForTimeout(700);
  assert.equal(await page.locator('#page-demo').isVisible(), true);
  assert.equal(await page.locator('#tour-bar').isVisible(), false);
  assert.equal(await page.locator('.tour-target').count(), 0);
  assert.equal(await page.locator('#featured-detail').isVisible(), false);
});

test('390px viewport keeps four-stop controls usable', async t => {
  const page = await pageFor(t, { width: 390, height: 844 });
  await page.goto(base + '/?recording=1&tour=1');
  for (let i = 1; i <= 4; i++) {
    await ready(page, i);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const rect = await page.locator('#tour-exit').boundingBox();
    assert.ok(rect.y >= 0 && rect.y + rect.height <= 844);
    if (i < 4) await page.locator('#tour-next').click();
  }
  await page.locator('#tour-exit').click();
});
