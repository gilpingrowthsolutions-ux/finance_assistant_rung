// Money (Accounts + Transactions + Bills + Cash Flow) beta qualification —
// Scenario A: setup/empty state, desktop and mobile.
//
// Requires a dev server already running against an explicit disposable
// RUNG_DB_PATH with RUNG_ENV=beta, seeded via seed_money_fresh.py
// (money-fresh@example.com / money-pass-123). The fixture deliberately has
// no Account, Bill, UserSetting, UserPreference, or IncomePlanVersion rows,
// so the point of this spec is that Money tells the truth about that rather
// than fabricating figures or activity.
const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5321';

async function login(page, email, password) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill(email);
    await page.locator('#authPassword').fill(password);
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('Loading your plan…');
  // A never-onboarded household is routed to onboarding first (opened
  // asynchronously after its own defaults fetch, shortly after networkidle);
  // "Set up later" is the real, truthful dismissal path (see the
  // Onboarding Slice 8 qualification) used here to reach Money's own
  // setup-needed state.
  const onboardingDialog = page.locator('#onboardingDialog');
  await onboardingDialog.waitFor({ state: 'visible', timeout: 4000 }).catch(() => {});
  if (await onboardingDialog.isVisible()) {
    await page.locator('#onboardingSkipAllBtn').click();
    await expect(onboardingDialog).not.toBeVisible();
  }
}

async function assertNoFabricatedRows(page) {
  const state = await page.evaluate(async () => ({
    txns: await fetch('/api/transactions').then((r) => r.json()),
    bills: await fetch('/bills').then((r) => r.json()),
    summary: await fetch('/api/budget/summary').then((r) => r.json()),
  }));
  expect(state.txns).toEqual([]);
  expect(state.bills).toEqual([]);
  expect(state.summary.safe_to_spend.state).toBe('needs_setup');
  expect(state.summary.account_state.checking_balance).toBeNull();
}

test('Scenario A desktop: Money truthfully shows setup-needed with zero fabricated activity', async ({ page }) => {
  const mutations = [];
  page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page, 'money-fresh@example.com', 'money-pass-123');
  mutations.length = 0; // login is the only expected mutation so far

  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#transactions')).toBeVisible();
  await expect(page.locator('#moneyCheckingBalance')).toHaveText('—');
  await expect(page.locator('#moneySafeToSpend')).toHaveText('Setup needed');
  await expect(page.locator('#moneyNeedsTotal')).toHaveText('Setup needed');
  await expect(page.locator('#moneyProtectedBuffer')).toHaveText('Setup needed');

  await page.locator('[data-money-view="activity"]').click();
  await expect(page.locator('#transactionList')).toContainText('No transactions yet');
  await page.locator('[data-money-view="bills"]').click();
  await expect(page.locator('#billsList')).toContainText('No Bills yet');
  await page.locator('[data-money-view="cashflow"]').click();
  await expect(page.locator('[data-money-panel="cashflow"]')).toBeVisible();

  // Cross-screen navigation and reload must not fabricate anything either.
  await page.locator('[data-target="overview"]').click();
  await expect(page.locator('#overview')).toBeVisible();
  await page.locator('[data-target="transactions"]').click();
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#moneyCheckingBalance')).toHaveText('—');

  await assertNoFabricatedRows(page);
  expect(mutations, 'viewing empty Money performs no mutation').toEqual([]);
});

test('Scenario A mobile: setup-needed Money is usable with no horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'money-fresh@example.com', 'money-pass-123');
  await expect(page.locator('.mobile-topbar')).toBeVisible();
  await expect(page.locator('.sidebar .brand')).toBeHidden();

  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#transactions')).toBeVisible();
  await expect(page.locator('#moneyCheckingBalance')).toHaveText('—');
  await page.screenshot({ path: '/tmp/rung-money-empty-mobile.png', fullPage: true, animations: 'disabled' });

  for (const name of ['activity', 'bills', 'cashflow', 'accounts']) {
    await page.locator(`[data-money-view="${name}"]`).click();
    await expect(page.locator(`[data-money-panel="${name}"]`)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `no horizontal overflow on ${name}`).toBeLessThanOrEqual(1);
  }

  await assertNoFabricatedRows(page);
});
