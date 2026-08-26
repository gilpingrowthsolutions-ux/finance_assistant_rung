// Overview + Safe-to-Spend beta qualification — Scenarios B-G.
//
// Scope: one financially-ready household (see seed_overview_stf_ready.py for
// the hand-calculated canonical arithmetic) exercised through the ACTUAL
// visible Overview/Money/Settings controls in sequence, proving each
// scenario's effect on the same canonical Safe-to-Spend authority. A second
// isolated household in the same fixture proves household isolation.
//
// Requires a dev server already running against an explicit disposable
// RUNG_DB_PATH with RUNG_ENV=beta, seeded via seed_overview_stf_ready.py
// (sts-ready@example.com / sts-pass-123, sts-iso@example.com / sts-pass-123).
const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5213';

function money(n) {
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function login(page, email, password) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill(email);
    await page.locator('#authPassword').fill(password);
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('Loading your plan…');
}

test.describe.serial('Overview Scenarios B-G (ready household)', () => {
  test('Scenario B: canonical Safe-to-Spend matches independently calculated backend inputs', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    mutations.length = 0; // login itself is the only expected mutation so far

    const canonical = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(canonical.safe_to_spend.safe_to_spend).toBe(700);
    expect(canonical.safe_to_spend.feasibility).toBe('full_target_feasible');
    expect(canonical.safe_to_spend.components.actual_forecast_needs).toBe(950);
    expect(canonical.safe_to_spend.components.protected_buffer).toBe(200);
    expect(canonical.safe_to_spend.feasible_savings_contribution).toBe(150);

    await expect(page.locator('#safeHeroAmount')).toHaveText(money(700));
    await expect(page.locator('#safeHeroState')).toHaveText('Safe to spend');
    await expect(page.locator('#kpiBalance')).toHaveText(money(2000));
    await expect(page.locator('#allocUnpaidAmt')).toHaveText(money(950));
    await expect(page.locator('#allocPyfAmt')).toHaveText(money(150));
    await expect(page.locator('#allocBufferAmt')).toHaveText(money(200));
    await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await expect(page.locator('#pyfStatusText')).toContainText('feasible this period');

    expect(mutations, 'viewing Overview performs no mutation').toEqual([]);
  });

  test('Scenario C: Update Balance is one intended mutation and recalculates Safe-to-Spend', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    mutations.length = 0;

    await page.locator('#overviewUpdateBalanceBtn').click();
    await expect(page.locator('#overviewBalanceDialog')).toBeVisible();
    await page.locator('#overviewBalanceInput').fill('2100.00');
    await page.locator('#overviewBalanceSave').click();
    await expect(page.locator('#overviewBalanceDialog')).not.toBeVisible();

    await expect(page.locator('#kpiBalance')).toHaveText(money(2100));
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(800));
    expect(mutations).toEqual(['/api/account/update']);

    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#kpiBalance')).toHaveText(money(2100));
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(800));
  });

  test('Scenario D: a new required Need increases Needs and decreases Safe-to-Spend without touching PYF/buffer', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    mutations.length = 0;

    await page.locator('[data-target="transactions"]').click();
    await page.locator('.money-tab[data-money-view="bills"]').click();
    await page.locator('#openBillDialog').click();
    await expect(page.locator('#moneyBillDialog')).toBeVisible();
    const dueDate = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10);
    await page.locator('#bName').fill('Internet');
    await page.locator('#bAmt').fill('80.00');
    await page.locator('#bDate').fill(dueDate);
    await page.locator('#moneyBillDialog button[type="submit"]').click();
    await expect(page.locator('#moneyBillDialog')).not.toBeVisible();
    expect(mutations).toEqual(['/bills']);

    await page.locator('[data-target="overview"]').click();
    await expect(page.locator('#allocUnpaidAmt')).toHaveText(money(1030));
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(720));
    await expect(page.locator('#allocPyfAmt')).toHaveText(money(150));
    await expect(page.locator('#allocBufferAmt')).toHaveText(money(200));
  });

  test('Scenario E: expected-paycheck change is next-cycle only and does not rewrite the active cycle', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    mutations.length = 0;

    await page.locator('[data-target="settings"]').click();
    await expect(page.locator('[data-settings-pane="financial"]')).toHaveClass(/is-active/);
    await expect(page.locator('#settingsExpectedPaycheck')).toHaveValue('1500.00');
    await page.locator('#settingsExpectedPaycheck').fill('1600.00');
    await page.locator('#updateRatiosBtn').click();
    await expect(page.locator('#settingsExpectedPaycheckStatus')).toContainText('next payday');
    expect(mutations).toEqual(['/api/account/update']);

    const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(summary.account_state.expected_paycheck).toBe(1500);
    expect(summary.account_state.next_expected_paycheck).toBe(1600);
    expect(summary.account_state.expected_paycheck_authority).toBe('income_plan_v1');
    // The active cycle's PYF target/feasibility is unchanged: still based on
    // the $1,500 current-cycle plan, not the $1,600 pending one.
    expect(summary.safe_to_spend.safe_to_spend).toBe(720);
    expect(summary.safe_to_spend.feasible_savings_contribution).toBe(150);

    await page.locator('[data-target="overview"]').click();
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(720));

    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(720));
  });

  test('Scenario F: one discretionary expense decreases Safe-to-Spend exactly once and appears in activity', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    mutations.length = 0;

    await page.locator('[data-target="transactions"]').click();
    await page.locator('.money-tab[data-money-view="activity"]').click();
    await page.locator('#openTransactionDialog').click();
    await expect(page.locator('#moneyTransactionDialog')).toBeVisible();
    await page.locator('#tDesc').fill('Family dinner');
    await page.locator('#tAmt').fill('60.00');
    await page.locator('#moneyTransactionDialog button[type="submit"]').click();
    await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
    expect(mutations).toEqual(['/api/transactions']);

    await expect(page.locator('#transactionList')).toContainText('Family dinner');
    await page.locator('[data-target="overview"]').click();
    await expect(page.locator('#kpiBalance')).toHaveText(money(2040));
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(660));
    await expect(page.locator('#allocUnpaidAmt')).toHaveText(money(1030));
    await expect(page.locator('#allocPyfAmt')).toHaveText(money(150));

    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(660));
    const summary = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(summary.filter((t) => t.description === 'Family dinner')).toHaveLength(1);
  });

  test('Ahead/Behind stays informational and never becomes spending permission', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    await page.locator('[data-target="transactions"]').click();
    await page.locator('.money-tab[data-money-view="cashflow"]').click();
    await expect(page.locator('#paycheckTimelinePanel')).toContainText('Ahead/Behind is informational. It does not change Safe-to-Spend or grant additional spending permission.');
    await page.locator('[data-target="overview"]').click();
    // Whatever the trajectory card says, the canonical hero amount is
    // unaffected by it.
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(660));
  });

  test('Household isolation: a second household never sees the ready household\'s numbers', async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'sts-iso@example.com', 'sts-pass-123');
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(410));
    await expect(page.locator('#allocUnpaidAmt')).toHaveText(money(0));
    await expect(page.locator('#kpiBalance')).toHaveText(money(500));

    const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(summary.safe_to_spend.safe_to_spend).toBe(410);
    expect(summary.account_state.checking_balance).toBe(500);

    // Logout leaves no residual session state usable for the next fetch.
    await page.locator('[data-target="settings"]').click();
    await page.locator('[data-settings-section="account"]').click();
    await page.locator('#logoutBtn').click();
    await expect(page.locator('#authDialog')).toBeVisible();
    const afterLogout = await page.evaluate(async () => (await fetch('/api/budget/summary')).status);
    expect(afterLogout).toBe(401);
    await context.close();
  });

  test('Scenario G: mobile responsive — Safe-to-Spend stays primary with no overflow', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page, 'sts-ready@example.com', 'sts-pass-123');
    await expect(page.locator('#safeHeroAmount')).toHaveText(money(660));
    await expect(page.locator('#safeHeroAmount')).toBeInViewport();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.locator('[data-target="transactions"]').click();
    await expect(page.locator('#transactions')).toBeVisible();
    await page.locator('[data-target="overview"]').click();
    await page.screenshot({ path: '/tmp/rung-overview-stf-scenario-g-mobile.png', fullPage: true, animations: 'disabled' });
  });
});
