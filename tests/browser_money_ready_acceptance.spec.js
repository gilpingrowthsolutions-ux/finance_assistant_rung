// Money (Accounts + Transactions + Bills + Cash Flow) beta qualification —
// Scenarios B-H plus household isolation and a mobile responsive pass.
//
// Scope: one financially-ready household (see seed_money_ready.py for the
// hand-calculated canonical Safe-to-Spend arithmetic) exercised through the
// ACTUAL visible Money controls in sequence, proving each scenario's effect
// on the same canonical Safe-to-Spend authority used by Overview. A second
// isolated household in the same fixture proves household isolation.
//
// Requires a dev server already running against an explicit disposable
// RUNG_DB_PATH with RUNG_ENV=beta, seeded via seed_money_ready.py
// (money-ready@example.com / money-pass-123, money-iso@example.com /
// money-pass-123).
const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5311';

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
  } else {
    // Disposable SQLite acceptance runs in development mode because beta mode
    // correctly requires PostgreSQL. Establish the same session explicitly
    // before exercising the visible Money controls.
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
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('Loading your plan…');
}

async function goToMoney(page) {
  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#transactions')).toBeVisible();
}

test.describe.serial('Money Scenarios B-H (ready household)', () => {
  test('Scenario B: manual expense is exactly one mutation and recalculates Safe-to-Spend once', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0; // login itself is the only expected mutation so far

    const initial = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(initial.account_state.checking_balance).toBe(2000);
    expect(initial.safe_to_spend.needs_total).toBe(1020);
    expect(initial.safe_to_spend.components.protected_buffer).toBe(200);
    expect(initial.safe_to_spend.feasible_savings_contribution).toBe(150);
    expect(initial.safe_to_spend.safe_to_spend).toBe(630);

    await goToMoney(page);
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(2000));
    await expect(page.locator('#moneySafeToSpend')).toHaveText(money(630));

    await page.locator('[data-money-view="activity"]').click();
    await expect(page.locator('#transactionList .list-item')).toHaveCount(1); // seeded "Existing pharmacy pickup"

    await page.locator('#openTransactionDialog').click();
    await expect(page.locator('#moneyTransactionDialog')).toBeVisible();
    await page.locator('#tDesc').fill('Money Scenario B purchase');
    await page.locator('#tAmt').fill('30.00');
    await page.locator('#tCat').selectOption('discretionary');
    // Fire two synchronous clicks (a real double-click race) to prove the
    // submit-button guard, not just a single deliberate click, protects
    // against a duplicate POST.
    await page.evaluate(() => {
      const btn = document.querySelector('#logExpenseForm button[type="submit"]');
      btn.click();
      btn.click();
    });
    await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();

    expect(mutations.filter((r) => r.path === '/api/transactions' && r.method === 'POST')).toHaveLength(1); // duplicate submit protected
    await expect(page.locator('#transactionList .list-item')).toHaveCount(2);
    await expect(page.locator('#transactionList')).toContainText('Money Scenario B purchase');

    const afterExpense = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(afterExpense.account_state.checking_balance).toBe(1970);
    expect(afterExpense.safe_to_spend.needs_total).toBe(1020); // Needs untouched by a discretionary spend
    expect(afterExpense.safe_to_spend.safe_to_spend).toBe(600); // down exactly $30
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(1970));
    await expect(page.locator('#moneySafeToSpend')).toHaveText(money(600));

    await page.reload({ waitUntil: 'networkidle' });
    await goToMoney(page);
    await page.locator('[data-money-view="activity"]').click();
    await expect(page.locator('#transactionList .list-item')).toHaveCount(2); // no duplicate on reload
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(1970));
  });

  test('Scenario C: manual income via Copilot is one economic effect and does not rewrite the expected-paycheck plan', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const before = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(before.safe_to_spend.period_income_cents).toBe(150000);

    await page.locator('[data-target="copilot"]').click();
    await page.locator('#copilotInput').fill('I got paid $400 from my side job');
    await page.locator('#copilotSendBtn').click();
    await expect(page.locator('#copilotStageDialog')).toBeVisible();
    expect(mutations.filter((r) => r.path === '/api/copilot/stage' && r.method === 'POST')).toHaveLength(1);
    await expect(page.locator('#copilotStageBody')).toContainText('Pending income log');
    await expect(page.locator('#copilotStageBody input[data-stage-field="amount"]')).toHaveValue('400');

    await page.locator('#copilotApplyStageBtn').click();
    await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
    expect(mutations.filter((r) => r.path === '/api/copilot/apply' && r.method === 'POST')).toHaveLength(1);

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.account_state.checking_balance).toBe(2370); // 1970 + 400
    expect(after.safe_to_spend.needs_total).toBe(1020); // unchanged
    expect(after.safe_to_spend.safe_to_spend).toBe(1000); // up exactly $400
    expect(after.safe_to_spend.period_income_cents).toBe(150000); // expected-paycheck plan untouched by actual income

    const txns = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(txns.filter((t) => t.category === 'income')).toHaveLength(1);

    await page.reload({ waitUntil: 'networkidle' });
    const afterReload = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(afterReload.filter((t) => t.category === 'income')).toHaveLength(1); // no duplicate on reload
  });

  test('Protected Finished Shopping activity is managed elsewhere and direct deletion is rejected without effect', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;
    const before = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());

    await goToMoney(page);
    await page.locator('[data-money-view="activity"]').click();
    const row = page.locator('#transactionList .list-item', { hasText: 'Existing pharmacy pickup' });
    await expect(row).toHaveCount(1);
    await expect(row).toContainText('Managed elsewhere');
    await expect(row.locator('button[data-action="del"]')).toHaveCount(0);

    const protectedId = await page.evaluate(async () => {
      const rows = await fetch('/api/transactions').then((r) => r.json());
      return rows.find((r) => r.description === 'Existing pharmacy pickup').id;
    });
    const rejected = await page.evaluate(async (id) => {
      const response = await fetch('/transactions/' + id, { method: 'DELETE' });
      return { status: response.status, body: await response.json() };
    }, protectedId);
    expect(rejected.status).toBe(409);
    expect(rejected.body.error).toContain("can't be deleted here");
    expect(mutations.filter((r) => /^\/transactions\/\d+$/.test(r.path) && r.method === 'DELETE')).toHaveLength(1);

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.account_state.checking_balance).toBe(before.account_state.checking_balance);
    expect(after.safe_to_spend.safe_to_spend).toBe(before.safe_to_spend.safe_to_spend);
    await expect(row).toContainText('Managed elsewhere');
  });

  test('Scenario D: a new required Bill increases Needs and decreases Safe-to-Spend without manufacturing an actual transaction', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const before = await page.evaluate(async () => (await fetch('/api/transactions')).json());

    await goToMoney(page);
    await page.locator('[data-money-view="bills"]').click();
    await page.locator('#openBillDialog').click();
    await expect(page.locator('#moneyBillDialog')).toBeVisible();
    const dueDate = new Date(Date.now() + 8 * 86400000).toISOString().slice(0, 10);
    await page.locator('#bName').fill('Car Insurance');
    await page.locator('#bAmt').fill('120.00');
    await page.locator('#bDate').fill(dueDate);
    // Fire two synchronous clicks (a real double-click race) to prove the
    // submit-button guard protects against a duplicate Bill POST.
    await page.evaluate(() => {
      const btn = document.querySelector('#addBillForm button[type="submit"]');
      btn.click();
      btn.click();
    });
    await expect(page.locator('#moneyBillDialog')).not.toBeVisible();

    expect(mutations.filter((r) => r.path === '/bills' && r.method === 'POST')).toHaveLength(1); // duplicate submit protected
    await expect(page.locator('#billsList')).toContainText('Car Insurance');

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.safe_to_spend.needs_total).toBe(1140); // up exactly $120
    expect(after.safe_to_spend.safe_to_spend).toBe(880); // down exactly $120
    expect(after.account_state.checking_balance).toBe(2370); // untouched — forecast only

    const afterTx = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(afterTx.length).toBe(before.length); // no ExpenseTransaction manufactured from a future Bill

    const timeline = await page.evaluate(async () => (await fetch('/api/paycheck-timeline')).json());
    const insuranceEvent = timeline.events.find((e) => String(e.label || '').includes('Car Insurance'));
    expect(insuranceEvent).toBeTruthy();
    expect(insuranceEvent.state).toBe('upcoming_confirmed'); // a future unpaid Bill, never "completed"/actual
    expect(insuranceEvent.kind).toBe('obligation');

    await page.reload({ waitUntil: 'networkidle' });
    await goToMoney(page);
    await page.locator('[data-money-view="bills"]').click();
    await expect(page.locator('#billsList .list-item', { hasText: 'Car Insurance' })).toHaveCount(1); // no duplicate on reload
  });

  test('Scenario E: marking a Bill Paid removes the forecast Need without double-counting it as actual spend', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const beforeTx = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    const beforeChecking = (await page.evaluate(async () => (await fetch('/api/budget/summary')).json())).account_state.checking_balance;

    await goToMoney(page);
    await page.locator('[data-money-view="bills"]').click();
    const internetRow = page.locator('#billsList .list-item', { hasText: 'Internet' });
    await internetRow.locator('button[data-action="toggle"]').click();
    await expect(internetRow).toContainText('Paid');

    expect(mutations.filter((r) => /^\/bills\/\d+\/pay$/.test(r.path) && r.method === 'POST')).toHaveLength(1);

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.safe_to_spend.needs_total).toBe(1070); // down exactly $70 (Internet no longer forecast)
    expect(after.safe_to_spend.safe_to_spend).toBe(950); // up exactly $70
    expect(after.account_state.checking_balance).toBe(beforeChecking); // Mark Paid moves no money by itself

    const afterTx = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(afterTx.length).toBe(beforeTx.length); // no ExpenseTransaction manufactured merely by marking paid

    await page.reload({ waitUntil: 'networkidle' });
    await goToMoney(page);
    await page.locator('[data-money-view="bills"]').click();
    await expect(page.locator('#billsList .list-item', { hasText: 'Internet' })).toContainText('Paid'); // persists
  });

  test('Scenario F: deleting a transaction reverses its checking-balance effect exactly once', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const before = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(before.account_state.checking_balance).toBe(2370);

    await goToMoney(page);
    await page.locator('[data-money-view="activity"]').click();
    const targetRow = page.locator('#transactionList .list-item', { hasText: 'Money Scenario B purchase' });
    await expect(targetRow).toHaveCount(1);
    await targetRow.locator('button[data-action="del"]').click();
    await expect(page.locator('#transactionList')).not.toContainText('Money Scenario B purchase');

    expect(mutations.filter((r) => /^\/transactions\/\d+$/.test(r.path) && r.method === 'DELETE')).toHaveLength(1);

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.account_state.checking_balance).toBe(2400); // reversed exactly the deleted $30 expense
    expect(after.safe_to_spend.safe_to_spend).toBe(980); // up exactly $30

    await page.reload({ waitUntil: 'networkidle' });
    await goToMoney(page);
    await page.locator('[data-money-view="activity"]').click();
    await expect(page.locator('#transactionList')).not.toContainText('Money Scenario B purchase');
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(2400)); // no re-duplication after reload
  });

  test('Scenario G: balance reconciliation sets checking directly with no fabricated transaction', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const beforeTx = await page.evaluate(async () => (await fetch('/api/transactions')).json());

    await goToMoney(page);
    await page.locator('#moneyUpdateBalanceBtn').click();
    await expect(page.locator('#overviewBalanceDialog')).toBeVisible();
    await page.locator('#overviewBalanceInput').fill('2500.00');
    await page.locator('#overviewBalanceSave').click();
    await expect(page.locator('#overviewBalanceDialog')).not.toBeVisible();

    expect(mutations.filter((r) => r.path === '/api/account/update' && r.method === 'POST')).toHaveLength(1);
    expect(mutations).toHaveLength(1); // exactly one visible mutation, nothing else

    const after = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(after.account_state.checking_balance).toBe(2500);
    expect(after.safe_to_spend.needs_total).toBe(1070); // unaffected by a balance reconciliation
    expect(after.safe_to_spend.safe_to_spend).toBe(1080); // up exactly the $100 reconciliation delta
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(2500));

    const afterTx = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(afterTx.length).toBe(beforeTx.length); // reconciliation never creates a fake expense

    await page.reload({ waitUntil: 'networkidle' });
    await goToMoney(page);
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(2500)); // stable after reload
  });

  test('Scenario H: Cash Flow shows actual/forecast distinctions and stays read-only', async ({ page }) => {
    const mutations = [];
    page.on('request', (req) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push({ method: req.method(), path: new URL(req.url()).pathname }); });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    mutations.length = 0;

    const canonical = await page.evaluate(async () => ({
      summary: await fetch('/api/budget/summary').then((r) => r.json()),
      timeline: await fetch('/api/paycheck-timeline').then((r) => r.json()),
      recap: await fetch('/api/payday-recap').then((r) => r.json()),
    }));
    expect(canonical.timeline.cycle.available).toBe(true);
    expect(canonical.recap.informational_only).toBe(true);
    expect(canonical.recap.read_only).toBe(true);
    expect(canonical.recap.financial_mutations).toBe(false);
    expect(canonical.recap.current_safe_to_spend).toBe(canonical.summary.safe_to_spend.safe_to_spend);
    const carInsurance = canonical.timeline.events.find((e) => String(e.label || '').includes('Car Insurance'));
    expect(carInsurance.kind).toBe('obligation'); // a future Bill is never "need_actual"
    expect(carInsurance.state).toBe('upcoming_confirmed');
    const internet = canonical.timeline.events.find((e) => String(e.label || '').includes('Internet'));
    expect(internet.kind).toBe('obligation'); // paid Bills stay a Bill obligation on the timeline for context...
    expect(internet.state).toBe('completed'); // ...marked completed, never converted into its own actual-spend ("need_actual") event
    const internetActualEvents = canonical.timeline.events.filter((e) => String(e.label || '').includes('Internet') && e.kind === 'need_actual');
    expect(internetActualEvents).toHaveLength(0); // no double-count: no separate actual transaction was manufactured for it

    await goToMoney(page);
    await page.locator('[data-money-view="cashflow"]').click();
    await expect(page.locator('#timelineTrajectoryValue')).not.toHaveText('—');
    await expect(page.locator('#timelineStatus')).toBeVisible();
    await expect(page.locator('[data-money-panel="cashflow"] .money-information-note')).toContainText('Ahead/Behind is informational');
    await expect(page.locator('#paydayRecapPanel')).toBeVisible();
    await page.locator('#recurringWatchPanel').evaluate((node) => { node.parentElement.open = true; });
    await expect(page.locator('#recurringWatchPanel')).toBeVisible();

    expect(mutations, 'viewing Cash Flow performs no mutation').toEqual([]);
  });

  test('Household isolation: a second household never sees the ready household\'s Money data', async ({ browser }) => {
    const page = await browser.newPage();
    await login(page, 'money-iso@example.com', 'money-pass-123');

    const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(summary.account_state.checking_balance).toBe(333);
    expect(summary.safe_to_spend.needs_total).toBe(0);
    expect(summary.safe_to_spend.safe_to_spend).toBe(238);

    const txns = await page.evaluate(async () => (await fetch('/api/transactions')).json());
    expect(txns).toEqual([]); // never sees the ready household's transactions
    const bills = await page.evaluate(async () => (await fetch('/bills')).json());
    expect(bills).toEqual([]); // never sees the ready household's Bills

    await goToMoney(page);
    await page.locator('[data-money-view="activity"]').click();
    await expect(page.locator('#transactionList')).toContainText('No transactions yet');
    await page.locator('[data-money-view="bills"]').click();
    await expect(page.locator('#billsList')).toContainText('No Bills yet');
    await expect(page.locator('#billsList')).not.toContainText('Car Insurance');
    await expect(page.locator('#billsList')).not.toContainText('Rent');

    await page.close();
  });

  test('Mobile: Money retains its four-tab structure with real financial data and no horizontal overflow', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page, 'money-ready@example.com', 'money-pass-123');
    await expect(page.locator('.mobile-topbar')).toBeVisible();
    await expect(page.locator('.sidebar .brand')).toBeHidden();

    await goToMoney(page);
    await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(2500));
    await expect(page.locator('#moneyCheckingBalance')).toBeInViewport();
    await page.screenshot({ path: '/tmp/rung-money-mobile-accounts.png', fullPage: true, animations: 'disabled' });

    for (const name of ['activity', 'bills', 'cashflow', 'accounts']) {
      await page.locator(`[data-money-view="${name}"]`).click();
      await expect(page.locator(`[data-money-panel="${name}"]`)).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `no horizontal overflow on ${name}`).toBeLessThanOrEqual(1);
    }

    await page.locator('[data-money-view="activity"]').click();
    await page.locator('#openTransactionDialog').click();
    await expect(page.locator('#moneyTransactionDialog')).toBeVisible();
    await expect(page.locator('#tDesc')).toBeInViewport();
    await page.locator('#cancelTransactionDialog').click();
    await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();

    await page.locator('[data-money-view="bills"]').click();
    await page.locator('#openBillDialog').click();
    await expect(page.locator('#moneyBillDialog')).toBeVisible();
    await page.locator('#cancelBillDialog').click();
    await expect(page.locator('#moneyBillDialog')).not.toBeVisible();

    // Cancelling both dialogs must not have mutated anything.
    const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
    expect(summary.account_state.checking_balance).toBe(2500);
  });
});
