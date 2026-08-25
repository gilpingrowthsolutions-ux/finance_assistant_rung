const { test, expect } = require('@playwright/test');

const BASE = 'http://127.0.0.1:5053/';

async function login(page, viewport) {
  await page.setViewportSize(viewport);
  await page.goto(BASE, { waitUntil:'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('recap-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('—');
}

function monitor(page) {
  const mutations = [], failures = [], consoleErrors = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!['GET','HEAD','OPTIONS'].includes(request.method())) mutations.push({method:request.method(), path});
  });
  page.on('response', response => {
    if (response.url().startsWith(BASE) && response.status() >= 400) failures.push({status:response.status(), path:new URL(response.url()).pathname});
  });
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  return {mutations, failures, consoleErrors};
}

test('Savings desktop preserves preview/review/apply and canonical destinations', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  seen.mutations.length = 0;
  await page.locator('[data-target="savings"]').click();
  await expect(page.locator('#savings')).toBeVisible();
  await expect(page.locator('#savingsPyfRing')).toHaveText('20%');
  await expect(page.locator('#savingsPyfStatus')).toContainText('feasible this pay cycle');
  const initial = await page.evaluate(async () => ({state:await fetch('/api/savings/state').then(r=>r.json()), budget:await fetch('/api/budget/summary').then(r=>r.json())}));
  await expect(page.locator('#reserveCards')).toContainText(initial.state.reserves[0].name);
  await expect(page.locator('#flexibleSavingsAmount')).toHaveText('$0.00');
  await expect(page.locator('#wealthCashAmount')).toHaveText('$0.00');
  await expect(page.locator('#wealthInvestmentAmount')).toHaveText('$0.00');

  await page.locator('#previewSavingsAllocation').click();
  await expect(page.locator('#allocationDialog')).toBeVisible();
  await expect(page.locator('#allocationPreview')).toContainText('Current feasible savings');
  await page.screenshot({path:'/tmp/rung-ui-slice3-allocation-dialog.png',animations:'disabled'});
  expect(seen.mutations).toHaveLength(0);
  const afterPreview = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(afterPreview).toEqual(initial.state);
  await page.locator('#cancelSavingsAllocation').click();
  await expect(page.locator('#allocationDialog')).not.toBeVisible();
  expect(seen.mutations).toHaveLength(0);

  await page.locator('#previewSavingsAllocation').click();
  await expect(page.locator('#confirmSavingsAllocation')).toBeEnabled();
  await page.locator('#confirmSavingsAllocation').click();
  await expect(page.locator('#allocationDialog')).not.toBeVisible();
  expect(seen.mutations.filter(row => row.path === '/api/savings/allocation/apply')).toHaveLength(1);
  const afterApply = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(afterApply.reserves[0].funded_cents).toBeGreaterThan(initial.state.reserves[0].funded_cents);
  const afterBudget = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  expect(afterBudget.account_state.checking_balance).toBe(initial.budget.account_state.checking_balance);
  await page.locator('.content').evaluate(node => node.scrollTop = 0);
  await page.screenshot({path:'/tmp/rung-ui-slice3-savings-desktop.png',fullPage:true,animations:'disabled'});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.locator('[data-target="transactions"]').click(); await expect(page.locator('#transactions')).toBeVisible();
  await page.locator('[data-target="overview"]').click(); await expect(page.locator('#overview')).toBeVisible();
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('Goals desktop creates, edits, pauses, reloads, and leaves Safe-to-Spend unchanged', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  seen.mutations.length = 0;
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  await page.locator('[data-target="goals"]').click();
  await page.locator('#openGoalDialog').click();
  await expect(page.locator('#goalDialog')).toBeVisible();
  await page.screenshot({path:'/tmp/rung-ui-slice3-goal-dialog.png',animations:'disabled'});
  await page.locator('#goalName').fill('Slice 3 Goal');
  await page.locator('#goalTarget').fill('900');
  await page.locator('#goalDate').fill('2027-01-15');
  await page.locator('#goalPriority').fill('3');
  await page.locator('#goalForm button[type="submit"]').click();
  await expect(page.locator('#goalCards')).toContainText('Slice 3 Goal');
  expect(seen.mutations.filter(row=>row.path==='/api/goals'&&row.method==='POST')).toHaveLength(1);
  await page.locator('.goal-edit').filter({hasText:'Edit plan'}).last().click();
  await expect(page.locator('#goalDialogTitle')).toHaveText('Edit Goal');
  await page.locator('#goalTarget').fill('950');
  await page.locator('#goalPriority').fill('2');
  await page.locator('#goalForm button[type="submit"]').click();
  await expect(page.locator('#goalCards')).toContainText('$0.00 of $950.00');
  await page.locator('.goal-status').last().click();
  await expect(page.locator('#goalCards')).toContainText('paused');
  expect(seen.mutations.filter(row=>row.path.startsWith('/api/goals/')&&row.method==='PATCH')).toHaveLength(2);
  const after = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  expect(after.safe_to_spend.safe_to_spend).toBe(before.safe_to_spend.safe_to_spend);
  await page.reload({waitUntil:'networkidle'}); await page.locator('[data-target="goals"]').click();
  await expect(page.locator('#goalCards')).toContainText('$0.00 of $950.00');
  await page.screenshot({path:'/tmp/rung-ui-slice3-goals-desktop.png',fullPage:true,animations:'disabled'});
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('Savings and Goals are readable through mobile More without overflow', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:390,height:844});
  await page.locator('#mobileMoreBtn').click();
  await page.locator('.mobile-more-destination[data-destination="savings"]').click();
  await expect(page.locator('#savings')).toBeVisible();
  await expect(page.locator('.sidebar .brand')).toBeHidden();
  await expect(page.locator('#savingsPyfStatus')).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({path:'/tmp/rung-ui-slice3-savings-mobile.png',fullPage:true,animations:'disabled'});
  await page.locator('#mobileMoreBtn').click();
  await page.locator('.mobile-more-destination[data-destination="goals"]').click();
  await expect(page.locator('#goals')).toBeVisible();
  await expect(page.locator('#openGoalDialog')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({path:'/tmp/rung-ui-slice3-goals-mobile.png',fullPage:true,animations:'disabled'});
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});
