// Feature 6 Copilot beta qualification. Requires the disposable database
// seeded by tests/seed_money_ready.py and a locally served Rung instance.
const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5056';

async function login(page, email, password) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  const loggedIn = await page.evaluate(async ({ email, password }) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    return response.ok;
  }, { email, password });
  expect(loggedIn).toBe(true);
  await page.reload({ waitUntil: 'networkidle' });
}

async function snapshot(page) {
  return page.evaluate(async () => {
    const [budget, transactions, bills, grocery, shopping] = await Promise.all([
      fetch('/api/budget/summary').then(r => r.json()),
      fetch('/api/transactions').then(r => r.json()),
      fetch('/bills').then(r => r.json()),
      fetch('/api/grocery').then(r => r.json()),
      fetch('/api/shopping/current-cart').then(r => r.json()),
    ]);
    return {
      checking: budget.account_state.checking_balance,
      safe: budget.safe_to_spend.safe_to_spend,
      transactionCount: transactions.length,
      billCount: bills.length,
      groceryCount: grocery.length,
      selectedStore: shopping.selected_store,
      cart: shopping.cart,
    };
  });
}

test('Feature 6: served Copilot preserves review, replay, and household boundaries', async ({ page }) => {
  test.setTimeout(60000);
  const errors = [], pageErrors = [], failed = [], requests = [], controlledResponses = [];
  let approvedPayload = null;
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('requestfailed', request => failed.push(request.url()));
  page.on('request', request => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      const path = new URL(request.url()).pathname;
      requests.push({ method: request.method(), path });
      if (path === '/api/copilot/apply' && !approvedPayload) approvedPayload = request.postDataJSON();
    }
  });
  page.on('response', response => {
    if (response.status() >= 400) controlledResponses.push({ status: response.status(), path: new URL(response.url()).pathname });
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page, 'money-ready@example.com', 'money-pass-123');
  await page.locator('[data-target="copilot"]').click();
  await expect(page.locator('#copilot')).toBeVisible();
  requests.length = 0;
  const before = await snapshot(page);

  // Canonical read-only affordability answer: no review dialog and no
  // consequential financial, cart, store, bill, or grocery mutation.
  await page.locator('#copilotInput').fill('Can I spend $50 tonight?');
  await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotThread')).toContainText('Safe-to-Spend');
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  expect(await snapshot(page)).toEqual(before);

  // A staged expense has a truthful review but no economic effect. Discard is
  // client-side cancellation and cannot apply after reload.
  await page.locator('#copilotInput').fill('I spent $25 at Coffee Shop');
  await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotStageDialog')).toBeVisible();
  await expect(page.locator('input[data-stage-section="expenses_logged"][data-stage-field="description"]')).toHaveValue(/Coffee Shop/);
  expect(await snapshot(page)).toEqual(before);
  await page.locator('#copilotDiscardStageBtn').click();
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-target="copilot"]').click();
  expect(await snapshot(page)).toEqual(before);

  // Confirmation through the actual review UI has exactly one effect even
  // under synchronous double click; the captured approved request replays
  // idempotently without a second effect.
  await page.locator('#copilotInput').fill('I spent $25 at Coffee Shop');
  await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotStageDialog')).toBeVisible();
  await page.evaluate(() => {
    const button = document.querySelector('#copilotApplyStageBtn');
    button.click(); button.click();
  });
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  await expect.poll(async () => (await snapshot(page)).transactionCount).toBe(before.transactionCount + 1);
  const afterApply = await snapshot(page);
  expect(afterApply.checking).toBe(before.checking - 25);
  expect(afterApply.safe).toBe(before.safe - 25);
  expect(afterApply.billCount).toBe(before.billCount);
  expect(afterApply.groceryCount).toBe(before.groceryCount);
  expect(afterApply.selectedStore).toEqual(before.selectedStore);
  expect(afterApply.cart).toEqual(before.cart);
  expect(requests.filter(r => r.path === '/api/copilot/apply')).toHaveLength(1);
  expect(approvedPayload).toBeTruthy();

  const replay = await page.evaluate(payload => fetch('/api/copilot/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(async response => ({ status: response.status, body: await response.json() })), approvedPayload);
  expect(replay.status).toBe(200);
  expect(replay.body.actions_taken.already_applied).toBe(true);
  expect(await snapshot(page)).toEqual(afterApply);

  // Insufficient deterministic input remains non-consequential and never
  // opens an approval review merely because a financial word was recognized.
  await page.locator('#copilotInput').fill('I usually spend on gas');
  await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  expect(await snapshot(page)).toEqual(afterApply);

  // Household B cannot apply the copied reviewed payload from A. Its state
  // remains its own, and the controlled 409 does not mutate either household.
  await page.evaluate(() => fetch('/api/auth/logout', { method: 'POST' }));
  await login(page, 'money-iso@example.com', 'money-pass-123');
  const bBefore = await snapshot(page);
  const crossHousehold = await page.evaluate(payload => fetch('/api/copilot/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(async response => ({ status: response.status, body: await response.json() })), approvedPayload);
  expect(crossHousehold.status).toBe(409);
  expect(crossHousehold.body.error).toContain('different household');
  expect(await snapshot(page)).toEqual(bBefore);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.nav-link[data-target="copilot"]').first().click();
  await expect(page.locator('#copilotInput')).toBeVisible();
  await page.locator('#copilotInput').fill('I got paid $40 from a side job');
  await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotStageDialog')).toBeVisible();
  await expect(page.locator('#copilotStageBody')).toContainText('Pending income log');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.locator('#copilotDiscardStageBtn').click();

  expect(controlledResponses).toEqual([{ status: 409, path: '/api/copilot/apply' }]);
  expect(errors).toEqual(['Failed to load resource: the server responded with a status of 409 (CONFLICT)']);
  expect(pageErrors).toEqual([]);
  expect(failed).toEqual([]);
});
