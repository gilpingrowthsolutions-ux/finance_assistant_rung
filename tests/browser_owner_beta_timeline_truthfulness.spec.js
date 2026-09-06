// Requires a fresh disposable RUNG_DB_PATH with RUNG_ENV=beta, seeded by
// seed_owner_beta_timeline_acceptance.py, and a served app at RUNG_UI_BASE_URL.
const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5053';
const FIXED_CYCLE = { start_date: '2026-08-21', end_date: '2026-09-04', end_exclusive: true };

test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH || '/home/ky/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome' } });

function cycleLabel(cycle) {
  const display = (value, options) => {
    const [, year, month, day] = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value);
    return new Date(Number(year), Number(month) - 1, Number(day), 12).toLocaleDateString('en-US', options);
  };
  const [, year, month, day] = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(cycle.end_date);
  const final = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  final.setUTCDate(final.getUTCDate() - 1);
  const finalDate = `${final.getUTCFullYear()}-${String(final.getUTCMonth() + 1).padStart(2, '0')}-${String(final.getUTCDate()).padStart(2, '0')}`;
  return `${display(cycle.start_date, { month: 'short', day: 'numeric' })} through ${display(finalDate, { month: 'short', day: 'numeric', year: 'numeric' })}`;
}

async function login(page) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('owner-beta-timeline@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
}

async function openBills(page) {
  await page.locator('[data-target="transactions"]').click();
  await page.getByRole('button', { name: 'Bills', exact: true }).click();
}

async function openTransactions(page) {
  await page.locator('[data-target="transactions"]').click();
  await page.getByRole('button', { name: 'Transactions', exact: true }).click();
}

test('owner-beta timeline gates future income without a global score', async ({ browser }) => {
  const centralContext = await browser.newContext({ timezoneId: 'America/Chicago' });
  const page = await centralContext.newPage();
  const consoleErrors = [], failedRequests = [], mutations = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('pageerror', err => consoleErrors.push(err.message));
  page.on('requestfailed', req => failedRequests.push(req.url()));
  page.on('request', req => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutations.push(new URL(req.url()).pathname); });
  await login(page);
  mutations.length = 0;

  const before = await page.evaluate(async () => ({
    budget: await fetch('/api/budget/summary').then(r => r.json()),
    timeline: await fetch('/api/paycheck-timeline').then(r => r.json()),
  }));
  expect(before.budget.safe_to_spend.safe_to_spend_cents).toBe(126057);
  expect(before.timeline.income.is_due).toBe(false);
  expect(before.timeline.income.unconfirmed_due_cents).toBe(0);
  expect(before.timeline).not.toHaveProperty('trajectory');
  expect(before.timeline.cycle.end_date).toBe(before.budget.safe_to_spend.next_expected_income.date.slice(0, 10));
  await expect(page.locator('#safeHeroAmount')).toHaveText('$1,260.57');
  await expect(page.locator('#overviewTrajectoryValue')).toHaveText('Cycle facts');
  await expect(page.locator('#overviewTrajectoryReason')).not.toContainText('behind');
  expect(mutations).toEqual([]);

  await expect(page.locator('#overviewCycleDates')).toHaveText(cycleLabel(before.timeline.cycle));
  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#timelineCycleDates')).toHaveText(cycleLabel(before.timeline.cycle));
  expect(mutations).toEqual([]);

  const westContext = await browser.newContext({ timezoneId: 'America/Los_Angeles' });
  const westPage = await westContext.newPage();
  await login(westPage);
  await expect(westPage.locator('#overviewCycleDates')).toHaveText(cycleLabel(before.timeline.cycle));
  await westContext.close();
  await centralContext.close();
  expect(failedRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test('served Money UI records one manual paycheck with one PYF consequence', async ({ page }) => {
  await login(page);
  await openTransactions(page);
  await page.locator('#openTransactionDialog').click();
  await page.locator('#tDesc').fill('Browser payroll');
  await page.locator('#tAmt').fill('1800');
  await page.locator('#tCat').selectOption('income');
  const requests = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/transactions' && request.method() === 'POST') {
      requests.push(JSON.parse(request.postData() || '{}'));
    }
  });
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  expect(requests).toHaveLength(1);
  expect(requests[0].operation_id).toBeTruthy();
  const state = await page.evaluate(async () => ({
    transactions: await fetch('/api/transactions').then(r => r.json()),
    budget: await fetch('/api/budget/summary').then(r => r.json()),
  }));
  expect(state.transactions.filter(row => row.description === 'Browser payroll')).toHaveLength(1);
  expect(state.budget.account_state.checking_balance).toBe(3600.57);
  expect(state.budget.safe_to_spend.components.active_income_pyf_protection_cents).toBe(18000);
});

test('served Bills UI keeps an orphaned recurring authority manageable beside an ordinary Bill', async ({ page }) => {
  await login(page);
  await openBills(page);
  await page.locator('#openBillDialog').click();
  await page.locator('#bName').fill('Browser repeating rent');
  await page.locator('#bAmt').fill('1200');
  await page.locator('#bDate').fill('2026-10-01');
  await page.locator('#bRecurrence').selectOption('monthly');
  await page.locator('#addBillForm button[type="submit"]').click();
  const linkedRow = page.locator('#billsList .list-item').filter({ hasText: 'Browser repeating rent' });
  await expect(linkedRow).toHaveCount(1);
  await expect(linkedRow).toContainText('Repeats · monthly');
  await linkedRow.getByRole('button', { name: 'Manage repeat' }).click();
  await expect(page.locator('#recurringBillDialog')).toBeVisible();
  await page.locator('#recurringBillAmount').fill('1250');
  await page.locator('#recurringBillDate').fill('2026-10-08');
  await page.locator('#recurringBillCadence').selectOption('weekly');
  await page.locator('#recurringBillForm button[type="submit"]').click();
  await expect(page.locator('#recurringBillDialog')).not.toBeVisible();
  await expect(linkedRow).toContainText('Repeats · weekly');

  // Ending a repeat is explicit and remains discoverable across reload.
  await linkedRow.getByRole('button', { name: 'Manage repeat' }).click();
  await page.locator('#recurringBillActive').uncheck();
  await page.locator('#recurringBillForm button[type="submit"]').click();
  await page.reload({ waitUntil: 'networkidle' });
  await openBills(page);
  const endedLinkedRow = page.locator('#billsList .list-item').filter({ hasText: 'Browser repeating rent' });
  await expect(endedLinkedRow).toContainText('Repeating Bill ended');
  await endedLinkedRow.getByRole('button', { name: 'Manage repeat' }).click();
  await expect(page.locator('#recurringBillActive')).not.toBeChecked();
  await page.locator('#recurringBillActive').check();
  await page.locator('#recurringBillForm button[type="submit"]').click();

  // Removing this explicit occurrence must not end or hide its separate
  // recurring authority; the seeded ordinary Bill remains in the same list.
  await page.locator('#billsList .list-item').filter({ hasText: 'Browser repeating rent' }).getByRole('button', { name: 'Remove' }).click();
  await page.reload({ waitUntil: 'networkidle' });
  await openBills(page);
  await expect(page.locator('#billsList')).toContainText('Required transportation');
  const orphanRow = page.locator('#billsList .list-item').filter({ hasText: 'Browser repeating rent' });
  await expect(orphanRow).toHaveCount(1);
  await expect(orphanRow).toContainText('Next due 2026-10-08');
  await orphanRow.getByRole('button', { name: 'Manage repeat' }).click();
  await expect(page.locator('#recurringBillActive')).toBeChecked();
  await page.locator('#recurringBillActive').uncheck();
  await page.locator('#recurringBillForm button[type="submit"]').click();
  await page.reload({ waitUntil: 'networkidle' });
  await openBills(page);
  await expect(page.locator('#billsList .list-item').filter({ hasText: 'Browser repeating rent' })).toContainText('Ended');
});
