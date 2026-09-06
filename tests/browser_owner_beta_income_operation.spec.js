const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5051';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });

async function loginAndOpenMoney(page) {
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('physical-transfer-browser@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  await page.locator('[data-target="transactions"]').click();
  await page.locator('[data-money-view="activity"]').click();
}

async function beginIncome(page, description, amount) {
  await page.locator('#openTransactionDialog').click();
  await page.locator('#tDesc').fill(description);
  await page.locator('#tAmt').fill(amount);
  await page.locator('#tCat').selectOption('income');
}

async function incomeCount(page) {
  return page.evaluate(() => fetch('/api/transactions').then(r => r.json()).then(rows => rows.filter(r => r.category === 'income').length));
}

test('Money rapid duplicate income keeps one operation and one PYF consequence', async ({ page }) => {
  await loginAndOpenMoney(page);
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  const bodies = [];
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/transactions' && r.method() === 'POST') bodies.push(r.postDataJSON()); });
  await beginIncome(page, 'Rapid income', '200');
  const submit = page.locator('#logExpenseForm button[type="submit"]');
  await Promise.all([submit.click(), submit.click()]);
  await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  expect(bodies).toHaveLength(1);
  expect(bodies[0].operation_id).toBeTruthy();
  expect(await incomeCount(page)).toBe(2);
  const after = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  expect(after.account_state.checking_balance).toBe(before.account_state.checking_balance + 200);
  const savings = await page.evaluate(() => fetch('/api/savings/state').then(r => r.json()));
  expect(savings.pyf_transfer_options.protections).toHaveLength(2);
});

test('Money response-loss retry retains the still-open income operation id', async ({ page }) => {
  await loginAndOpenMoney(page);
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  const bodies = []; let lost = false;
  await page.route('**/api/transactions', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    bodies.push(route.request().postDataJSON());
    if (!lost) { lost = true; await route.fetch(); await route.abort(); } else await route.continue();
  });
  await beginIncome(page, 'Response loss income', '200');
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#moneyTransactionDialog')).toBeVisible();
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  expect(bodies).toHaveLength(2);
  expect(bodies[0].operation_id).toBe(bodies[1].operation_id);
  expect(await incomeCount(page)).toBe(2);
  const after = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  expect(after.account_state.checking_balance).toBe(before.account_state.checking_balance + 200);
});

test('Money next distinct income intent gets a new operation id', async ({ page }) => {
  await loginAndOpenMoney(page);
  const bodies = [];
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/transactions' && r.method() === 'POST') bodies.push(r.postDataJSON()); });
  await beginIncome(page, 'First new income', '100');
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  await beginIncome(page, 'Second new income', '125');
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  expect(bodies).toHaveLength(2);
  expect(bodies[0].operation_id).not.toBe(bodies[1].operation_id);
  expect(await incomeCount(page)).toBe(3);
});
