const { test, expect } = require('@playwright/test');

const ROOT = 'http://127.0.0.1:5054';

async function login(page) {
  await page.goto(ROOT + '/', {waitUntil: 'networkidle'});
  await page.waitForFunction(() => document.querySelector('#authDialog')?.open || typeof window.rungOpenTab === 'function');
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('behavior-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await page.waitForLoadState('networkidle');
  }
}

async function financialSnapshot(page) {
  return page.evaluate(async () => {
    const [budget, bills, transactions, savings] = await Promise.all([
      fetch('/api/budget/summary').then(r => r.json()),
      fetch('/bills').then(r => r.json()),
      fetch('/api/transactions').then(r => r.json()),
      fetch('/api/savings/state').then(r => r.json()),
    ]);
    return {safe: budget.safe_to_spend.safe_to_spend_cents, bills, transactions, savings};
  });
}

test('Copilot staged apply remains reviewed, idempotent, and responsive', async ({page}) => {
  test.setTimeout(60000);
  const errors = [], failed = [], mutations = [];
  let confirmedPayload = null;
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  page.on('requestfailed', r => failed.push(r.url()));
  page.on('request', r => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method())) {
      const path = new URL(r.url()).pathname;
      mutations.push({method: r.method(), path});
      if (path === '/api/copilot/apply' && !confirmedPayload) confirmedPayload = r.postDataJSON();
    }
  });

  await login(page);
  mutations.length = 0;
  const before = await financialSnapshot(page);
  await page.locator('[data-target="transactions"]').click();
  const candidate = page.locator('#recurringWatchCards .behavior-card').filter({hasText: 'stream box'});

  // Staging and discard preserve the human-review boundary and write nothing.
  await candidate.locator('.behavior-stage-bill').click();
  await expect(page.locator('#copilotStageDialog')).toBeVisible();
  await expect(page.locator('input[data-stage-section="bills_added"][data-stage-field="name"]')).toHaveValue('Stream Box');
  await page.locator('#copilotDiscardStageBtn').click();
  expect((await financialSnapshot(page)).bills).toEqual(before.bills);

  await page.locator('[data-target="transactions"]').click();
  await candidate.locator('.behavior-stage-bill').click();
  await page.locator('#copilotApplyStageBtn').click();
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  await expect.poll(async () => (await financialSnapshot(page)).bills.length).toBe(before.bills.length + 1);
  expect(confirmedPayload).toBeTruthy();
  expect(mutations.filter(x => x.path === '/api/copilot/apply')).toHaveLength(1);

  const afterOne = await financialSnapshot(page);
  const retry = await page.evaluate(payload => fetch('/api/copilot/apply', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
  }).then(async r => ({status: r.status, body: await r.json()})), confirmedPayload);
  expect(retry.status).toBe(200);
  expect(retry.body.actions_taken.already_applied).toBe(true);
  expect(await financialSnapshot(page)).toEqual(afterOne);

  await page.reload({waitUntil: 'networkidle'});
  expect(await financialSnapshot(page)).toEqual(afterOne);
  await page.evaluate(() => window.rungOpenTab('copilot'));
  await page.screenshot({path: '/tmp/rung-copilot-concurrency-desktop.png', animations: 'disabled'});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await page.setViewportSize({width: 390, height: 844});
  await page.evaluate(() => window.rungOpenTab('copilot'));
  await expect(page.locator('#copilot')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({path: '/tmp/rung-copilot-concurrency-mobile.png', animations: 'disabled'});

  expect(mutations.filter(x => x.path === '/api/copilot/apply')).toHaveLength(2);
  expect(mutations.filter(x => /transactions|bills|goals|reserves|savings\/transfer|allocation\/apply/.test(x.path))).toEqual([]);
  expect(failed).toEqual([]);
  expect(errors).toEqual([]);
});
