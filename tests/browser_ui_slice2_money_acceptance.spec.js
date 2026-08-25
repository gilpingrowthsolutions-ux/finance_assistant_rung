const { test, expect } = require('@playwright/test');

const BASE = 'http://127.0.0.1:5052/';

async function openRung(page, viewport) {
  await page.setViewportSize(viewport);
  await page.goto(BASE, { waitUntil:'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('recap-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('—');
  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#transactions')).toBeVisible();
}

test('Money desktop uses canonical state and exactly one mutation per touched action', async ({ page }) => {
  const mutations = [];
  const failures = [];
  const consoleErrors = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!['GET','HEAD','OPTIONS'].includes(request.method())) mutations.push({ method:request.method(), path });
  });
  page.on('response', response => {
    if (response.url().startsWith(BASE) && response.status() >= 400) failures.push({ status:response.status(), path:new URL(response.url()).pathname });
  });
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });

  await openRung(page, { width:1440, height:1000 });
  mutations.length = 0;
  await expect(page.locator('.brand-wordmark')).toHaveText('Rung');
  await expect(page.locator('html')).toHaveAttribute('data-theme','light');
  const initial = await page.evaluate(async () => ({
    summary: await fetch('/api/budget/summary').then(r => r.json()),
    transactions: await fetch('/api/transactions').then(r => r.json()),
    bills: await fetch('/bills').then(r => r.json()),
    timeline: await fetch('/api/paycheck-timeline').then(r => r.json()),
    recap: await fetch('/api/payday-recap').then(r => r.json()),
    behavior: await fetch('/api/behavior-intelligence').then(r => r.json()),
  }));
  const money = value => '$' + Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(initial.summary.account_state.checking_balance));
  await expect(page.locator('#moneySafeToSpend')).toHaveText(money(initial.summary.safe_to_spend.safe_to_spend));
  await expect(page.locator('#moneyRecentTransactions .list-item')).toHaveCount(Math.min(4, initial.transactions.length));
  await expect(page.locator('#moneyUpcomingBills')).toContainText(initial.bills.find(row => !row.is_paid).name);
  await page.screenshot({ path:'/tmp/rung-ui-slice2-money-desktop.png', fullPage:true, animations:'disabled' });

  await page.locator('[data-money-view="activity"]').click();
  await expect(page.locator('#transactionList .list-item')).toHaveCount(initial.transactions.length);
  await page.locator('#openTransactionDialog').click();
  await expect(page.locator('#moneyTransactionDialog')).toBeVisible();
  await page.locator('#tDesc').fill('Slice 2 acceptance purchase');
  await page.locator('#tAmt').fill('12.34');
  await page.locator('#tCat').selectOption('discretionary');
  await page.locator('#logExpenseForm button[type="submit"]').click();
  await expect(page.locator('#transactionList')).toContainText('Slice 2 acceptance purchase');
  expect(mutations.filter(row => row.path === '/api/transactions' && row.method === 'POST')).toHaveLength(1);
  const afterTransaction = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(afterTransaction.account_state.checking_balance).toBe(Number((initial.summary.account_state.checking_balance - 12.34).toFixed(2)));

  await page.locator('[data-money-view="bills"]').click();
  await page.locator('#openBillDialog').click();
  await page.locator('#bName').fill('Slice 2 acceptance Bill');
  await page.locator('#bAmt').fill('44.00');
  await page.locator('#bDate').fill('2026-09-03');
  await page.locator('#addBillForm button[type="submit"]').click();
  await expect(page.locator('#billsList')).toContainText('Slice 2 acceptance Bill');
  expect(mutations.filter(row => row.path === '/bills' && row.method === 'POST')).toHaveLength(1);
  const afterBill = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(afterBill.account_state.checking_balance).toBe(afterTransaction.account_state.checking_balance);

  await page.locator('[data-money-view="cashflow"]').click();
  await expect(page.locator('#timelineTrajectoryValue')).not.toHaveText('—');
  await expect(page.locator('#timelineStatus')).not.toHaveText('Important events appear first.');
  expect(initial.timeline.status).toBe('available');
  expect(initial.recap.status).toBe('available');
  await expect(page.locator('#paydayRecapPanel')).toContainText('Informational only');
  expect(initial.behavior.recurring_candidates.length).toBeGreaterThan(0);
  await page.locator('#recurringWatchPanel').evaluate(node => node.parentElement.open = true);
  await expect(page.locator('#recurringWatchCards')).toContainText('planet fitness');
  await page.screenshot({ path:'/tmp/rung-ui-slice2-money-cashflow-desktop.png', fullPage:true, animations:'disabled' });

  await page.locator('#moneyUpdateBalanceBtn').click();
  await page.locator('#overviewBalanceInput').fill(String(afterTransaction.account_state.checking_balance + 25));
  await page.locator('#overviewBalanceSave').click();
  expect(mutations.filter(row => row.path === '/api/account/update' && row.method === 'POST')).toHaveLength(1);
  await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(afterTransaction.account_state.checking_balance + 25));
  expect(mutations).toHaveLength(3);

  await page.reload({ waitUntil:'networkidle' });
  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#moneyCheckingBalance')).toHaveText(money(afterTransaction.account_state.checking_balance + 25));
  await page.locator('[data-target="overview"]').click();
  await expect(page.locator('#overview')).toBeVisible();
  await page.locator('[data-target="transactions"]').click();
  expect(failures).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test('Money mobile is a readable primary destination without horizontal overflow', async ({ page }) => {
  await openRung(page, { width:390, height:844 });
  await expect(page.locator('.mobile-topbar')).toBeVisible();
  await expect(page.locator('.sidebar .brand')).toBeHidden();
  await expect(page.locator('#moneyCheckingBalance')).not.toHaveText('—');
  await expect(page.locator('#moneyCheckingBalance')).toBeInViewport();
  await page.screenshot({ path:'/tmp/rung-ui-slice2-money-mobile.png', fullPage:true, animations:'disabled' });
  for (const name of ['activity','bills','cashflow','accounts']) {
    await page.locator(`[data-money-view="${name}"]`).click();
    await expect(page.locator(`[data-money-panel="${name}"]`)).toBeVisible();
  }
  await page.locator('[data-money-view="activity"]').click();
  await expect(page.locator('#openTransactionDialog')).toBeVisible();
  await page.locator('[data-money-view="bills"]').click();
  await expect(page.locator('#openBillDialog')).toBeVisible();
  await page.locator('[data-money-view="accounts"]').click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
