const { test, expect } = require('playwright/test');

test('Package 18/20 auth, canonical Overview/Money, IA, and write acceptance', async ({ page }) => {
  const mutations = [];
  const apiRequests = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/') || path === '/bills') apiRequests.push({ method: request.method(), path });
    if (request.method() !== 'GET') mutations.push(path);
  });

  await page.goto('http://127.0.0.1:5051/', { waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();
  expect(apiRequests.filter(r => r.path === '/api/budget/summary')).toHaveLength(0);
  await expect(page.locator('#authDialog')).toContainText('Self-service account creation is not currently supported');

  mutations.length = 0;
  await page.locator('#authEmail').fill('browser@example.com');
  await page.locator('#authPassword').fill('wrong-password');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authError')).toContainText('not recognized');
  expect(mutations.filter(path => path === '/api/auth/login')).toHaveLength(1);

  mutations.length = 0;
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#authDialog')).not.toBeVisible();
  expect(mutations.filter(path => path === '/api/auth/login')).toHaveLength(1);

  const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(summary.authority).toBe('legacy_liquidity_compatibility_only');
  expect(summary.authoritative).toBe(false);
  expect(summary.safe_to_spend.authority).toBe('canonical_pyf_v1');
  expect(summary.safe_to_spend.safe_to_spend).toBe(735);
  expect(summary.safe_to_spend.components.actual_forecast_needs).toBe(615);
  expect(summary.safe_to_spend.feasible_savings_contribution).toBe(250);
  expect(summary.safe_to_spend.components.protected_buffer).toBe(150);
  expect(summary.safe_to_spend.components.bills_before_payday).toBe(300);
  expect(summary.safe_to_spend.components.bills_before_payday_count).toBe(1);
  expect(summary.food_budget).not.toBe(summary.safe_to_spend.components.groceries_remaining);

  await expect(page.locator('#safeHeroAmount')).toHaveText('$735.00');
  await expect(page.locator('#kpiUnpaid')).toHaveText('$300.00');
  await expect(page.locator('#kpiBillCount')).toHaveText('1');
  await expect(page.locator('#allocUnpaidAmt')).toHaveText('$615.00');
  await expect(page.locator('#allocPyfAmt')).toHaveText('$250.00');
  await expect(page.locator('#allocBufferAmt')).toHaveText('$150.00');
  await expect(page.locator('#allocSafeAmt')).toHaveText('$735.00');
  await expect(page.locator('#sumPaycheck')).toHaveText('$2,000.00');
  await expect(page.locator('#primaryNav')).not.toContainText('Can I Buy?');
  await expect(page.locator('#primaryNav')).toContainText('Money');
  await expect(page.locator('#primaryNav')).toContainText('Shopping');
  await page.locator('#overview .copilot-entry').filter({ hasText: 'Can I Afford This?' }).click();
  await expect(page.locator('#canibuy')).toBeVisible();

  await page.locator('[data-target="settings"]').click();
  await expect(page.locator('#settingsAccountEmail')).toHaveText('browser@example.com');
  await expect(page.locator('#settingsAccountRole')).toHaveText('owner');
  const txBeforeBalance = await page.evaluate(async () => (await fetch('/api/transactions')).json());
  mutations.length = 0;
  await page.locator('#settingsBalance').fill('1800');
  await page.locator('#updateBalanceBtn').click();
  await expect(page.locator('#settingsBalanceStatus')).toHaveText('Saved.');
  expect(mutations.filter(path => path === '/api/account/update')).toHaveLength(1);
  const afterBalance = await page.evaluate(async () => ({
    summary: await fetch('/api/budget/summary').then(r => r.json()),
    tx: await fetch('/api/transactions').then(r => r.json()),
  }));
  expect(afterBalance.summary.account_state.checking_balance).toBe(1800);
  expect(afterBalance.tx).toHaveLength(txBeforeBalance.length);

  await page.locator('[data-target="transactions"]').click();
  mutations.length = 0;
  await page.locator('#tDesc').fill('Browser coffee');
  await page.locator('#tAmt').fill('10.25');
  await page.locator('#tCat').selectOption('discretionary');
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect.poll(() => mutations.filter(path => path === '/api/transactions').length).toBe(1);
  const afterExpense = await page.evaluate(async () => ({
    summary: await fetch('/api/budget/summary').then(r => r.json()),
    tx: await fetch('/api/transactions').then(r => r.json()),
  }));
  expect(afterExpense.summary.account_state.checking_balance).toBe(1789.75);
  expect(afterExpense.tx.filter(row => row.description === 'Browser coffee')).toHaveLength(1);

  mutations.length = 0;
  await page.locator('#bName').fill('Browser internet');
  await page.locator('#bAmt').fill('45');
  await page.locator('#bDate').fill('2026-08-25');
  await page.locator('#addBillForm button[type="submit"]').click();
  await expect.poll(() => mutations.filter(path => path === '/bills').length).toBe(1);
  const bills = await page.evaluate(async () => (await fetch('/bills')).json());
  expect(bills.filter(row => row.name === 'Browser internet')).toHaveLength(1);

  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#safeHeroAmount')).toBeVisible();
  const persisted = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(persisted.account_state.checking_balance).toBe(1789.75);
  await page.locator('[data-target="settings"]').click();
  mutations.length = 0;
  await page.locator('#logoutBtn').click();
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#authDialog')).toBeVisible();
  expect(mutations.filter(path => path === '/api/auth/logout')).toHaveLength(1);
});

test('Package 18/20 desktop and mobile touched surfaces are usable', async ({ page }) => {
  await page.goto('http://127.0.0.1:5051/', { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('browser@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await page.waitForLoadState('networkidle');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.locator('#overview')).toBeVisible();
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('$0.00');
  await page.screenshot({ path: '/tmp/rung-package18-20-overview-desktop.png', animations: 'disabled', timeout: 10000 });
  await page.locator('[data-target="transactions"]').click();
  await page.screenshot({ path: '/tmp/rung-package18-20-money-desktop.png', animations: 'disabled', timeout: 10000 });

  await page.setViewportSize({ width: 390, height: 844 });
  for (const target of ['overview', 'transactions', 'settings']) {
    await page.locator(`[data-target="${target}"]`).click();
    const surface = await page.locator(`#${target}`).boundingBox();
    expect(surface.x).toBeGreaterThanOrEqual(0);
    expect(surface.x + surface.width).toBeLessThanOrEqual(391);
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.locator('[data-target="overview"]').click();
  await page.screenshot({ path: '/tmp/rung-package18-20-overview-mobile.png', animations: 'disabled', timeout: 10000 });
});
