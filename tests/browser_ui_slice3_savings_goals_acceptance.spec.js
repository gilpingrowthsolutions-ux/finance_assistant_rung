const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

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

async function loginAs(page, email, password) {
  await page.evaluate(() => fetch('/api/auth/logout', {method:'POST'}));
  const response = await page.evaluate(async ({email, password}) => {
    const result = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email,password})});
    return {status:result.status, body:await result.json()};
  }, {email, password});
  expect(response.status).toBe(200);
  expect(response.body.authenticated).toBeTruthy();
  await page.reload({waitUntil:'networkidle'});
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
  await expect(page.locator('#allocationPreview')).toContainText('Available to allocate this cycle');
  await page.screenshot({path:'/tmp/rung-ui-slice3-allocation-dialog.png',animations:'disabled'});
  expect(seen.mutations).toHaveLength(0);
  const afterPreview = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(afterPreview).toEqual(initial.state);
  await page.locator('#cancelSavingsAllocation').click();
  await expect(page.locator('#allocationDialog')).not.toBeVisible();
  expect(seen.mutations).toHaveLength(0);

  await page.locator('#previewSavingsAllocation').click();
  await expect(page.locator('#confirmSavingsAllocation')).toBeEnabled();
  const plannedGoal = (await page.evaluate(() => fetch('/api/savings/allocation/preview').then(r=>r.json()))).allocations.find(row => row.kind === 'goal');
  expect(plannedGoal).toBeTruthy();
  await page.locator('#confirmSavingsAllocation').click();
  await expect(page.locator('#allocationDialog')).not.toBeVisible();
  expect(seen.mutations.filter(row => row.path === '/api/savings/allocation/apply')).toHaveLength(1);
  const afterApply = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(afterApply.reserves[0].funded_cents).toBeGreaterThan(initial.state.reserves[0].funded_cents);
  const fundedGoal = afterApply.goals.find(goal => goal.destination_id === plannedGoal.destination_id);
  expect(fundedGoal.funded_cents).toBe(plannedGoal.amount_cents);
  expect(fundedGoal.percentage_funded).toBe(fundedGoal.funded_cents / fundedGoal.target_cents * 100);
  expect(fundedGoal.status).toBe('completed');
  const afterBudget = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  expect(afterBudget.account_state.checking_balance).toBe(initial.budget.account_state.checking_balance);
  expect(afterBudget.safe_to_spend.safe_to_spend).toBe(initial.budget.safe_to_spend.safe_to_spend);
  await page.locator('#previewSavingsAllocation').click();
  await expect(page.locator('#allocationPreview')).toContainText('Available to allocate this cycle: $0.00');
  await expect(page.locator('#confirmSavingsAllocation')).toBeDisabled();
  await page.locator('#cancelSavingsAllocation').click();
  await page.locator('.content').evaluate(node => node.scrollTop = 0);
  await page.screenshot({path:'/tmp/rung-ui-slice3-savings-desktop.png',fullPage:true,animations:'disabled'});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.locator('[data-target="transactions"]').click(); await expect(page.locator('#transactions')).toBeVisible();
  await page.locator('[data-target="overview"]').click(); await expect(page.locator('#overview')).toBeVisible();
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('Goals create is guarded against a recoverable failure and synchronous double-submit; edit/date persists', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  seen.mutations.length = 0;
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  await page.locator('[data-target="goals"]').click();
  await page.locator('#openGoalDialog').click();
  await expect(page.locator('#goalDialog')).toBeVisible();
  await page.screenshot({path:'/tmp/rung-ui-slice3-goal-dialog.png',animations:'disabled'});
  await page.locator('#goalName').fill('Retry-only Goal');
  await page.locator('#goalTarget').fill('900');
  await page.route('**/api/goals', route => route.fulfill({status:500, contentType:'application/json', body:JSON.stringify({error:'temporary test failure'})}));
  await page.locator('#goalForm button[type="submit"]').click();
  await expect(page.locator('#goalsStatus')).toContainText('temporary test failure');
  await expect(page.locator('#goalForm button[type="submit"]')).toBeEnabled();
  await page.unroute('**/api/goals');
  seen.mutations.length = 0; seen.failures.length = 0; seen.consoleErrors.length = 0;
  await page.locator('#goalName').fill('Slice 3 Goal');
  await page.locator('#goalTarget').fill('900');
  await page.locator('#goalDate').fill('2027-01-15');
  await page.locator('#goalPriority').fill('3');
  await page.locator('#goalForm button[type="submit"]').evaluate(button => { button.click(); button.click(); });
  await expect(page.locator('#goalCards')).toContainText('Slice 3 Goal');
  expect(seen.mutations.filter(row=>row.path==='/api/goals'&&row.method==='POST')).toHaveLength(1);
  const afterCreate = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(afterCreate.goals.filter(goal => goal.name === 'Slice 3 Goal')).toHaveLength(1);
  await page.locator('.goal-edit').filter({hasText:'Edit plan'}).last().click();
  await expect(page.locator('#goalDialogTitle')).toHaveText('Edit Goal');
  await page.locator('#goalName').fill('Slice 3 Goal Edited');
  await page.locator('#goalTarget').fill('950');
  await page.locator('#goalDate').fill('2027-02-20');
  await page.locator('#goalPriority').fill('2');
  await page.locator('#goalForm button[type="submit"]').click();
  await expect(page.locator('#goalCards')).toContainText('Slice 3 Goal Edited');
  await expect(page.locator('#goalCards')).toContainText('$0.00 of $950.00');
  await page.locator('.goal-status').last().click();
  await expect(page.locator('#goalCards')).toContainText('paused');
  expect(seen.mutations.filter(row=>row.path.startsWith('/api/goals/')&&row.method==='PATCH')).toHaveLength(2);
  const after = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  expect(after.safe_to_spend.safe_to_spend).toBe(before.safe_to_spend.safe_to_spend);
  await page.reload({waitUntil:'networkidle'}); await page.locator('[data-target="goals"]').click();
  await expect(page.locator('#goalCards')).toContainText('$0.00 of $950.00');
  await expect(page.locator('#goalCards')).toContainText('by 2027-02-20');
  await page.screenshot({path:'/tmp/rung-ui-slice3-goals-desktop.png',fullPage:true,animations:'disabled'});
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('Reserve create/use forms are guarded and reserve use is an explicit one-time ledger effect', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  await page.locator('[data-target="savings"]').click();
  const before = await page.evaluate(async () => ({state:await fetch('/api/savings/state').then(r=>r.json()), budget:await fetch('/api/budget/summary').then(r=>r.json())}));
  await page.locator('#openReserveDialog').click();
  await page.locator('#reserveName').fill('Double-submit Reserve');
  await page.locator('#reserveCategory').selectOption('vehicle');
  await page.locator('#reserveTarget').fill('80');
  await page.locator('#reserveForm button[type="submit"]').evaluate(button => { button.click(); button.click(); });
  await expect(page.locator('#reserveCards')).toContainText('Double-submit Reserve');
  expect(seen.mutations.filter(row => row.method === 'POST' && row.path === '/api/reserves')).toHaveLength(1);
  let state = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(state.reserves.filter(reserve => reserve.name === 'Double-submit Reserve')).toHaveLength(1);
  expect((await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()))).account_state.checking_balance).toBe(before.budget.account_state.checking_balance);
  expect((await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()))).safe_to_spend.safe_to_spend).toBe(before.budget.safe_to_spend.safe_to_spend);

  const funded = state.reserves.find(reserve => reserve.funded_cents > 0);
  expect(funded).toBeTruthy();
  const reserveBefore = funded.funded_cents;
  await page.locator(`.reserve-use[data-destination="${funded.destination_id}"]`).click();
  await expect(page.locator('#reserveUseDialog')).toBeVisible();
  await page.locator('#reserveUsePurpose').fill('emergency household repair');
  await page.locator('#reserveUseAmount').fill('10');
  await page.locator('#reserveUseForm button[type="submit"]').evaluate(button => { button.click(); button.click(); });
  await expect(page.locator('#reserveUseDialog')).not.toBeVisible();
  expect(seen.mutations.filter(row => row.method === 'POST' && row.path === '/api/savings/transfer')).toHaveLength(1);
  state = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(state.reserves.find(reserve => reserve.destination_id === funded.destination_id).funded_cents).toBe(reserveBefore - 1000);
  const afterUseBudget = await page.evaluate(() => fetch('/api/budget/summary').then(r=>r.json()));
  expect(afterUseBudget.account_state.checking_balance).toBe(before.budget.account_state.checking_balance);
  expect(afterUseBudget.safe_to_spend.safe_to_spend).toBe(before.budget.safe_to_spend.safe_to_spend);
  await page.reload({waitUntil:'networkidle'}); await page.locator('[data-target="savings"]').click();
  await expect(page.locator('#reserveCards')).toContainText('Double-submit Reserve');
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('No remaining allocation can fund a new Goal or silently raid a protected Reserve', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  await page.locator('[data-target="savings"]').click();
  const before = await page.evaluate(async () => ({state:await fetch('/api/savings/state').then(r=>r.json()), budget:await fetch('/api/budget/summary').then(r=>r.json()), plan:await fetch('/api/savings/allocation/preview').then(r=>r.json())}));
  expect(before.plan.remaining_available_cents).toBe(0);
  await page.locator('[data-target="goals"]').click();
  await page.locator('#openGoalDialog').click();
  await page.locator('#goalName').fill('Unfunded protected Goal');
  await page.locator('#goalTarget').fill('9999');
  await page.locator('#goalPriority').fill('0');
  await page.locator('#goalForm button[type="submit"]').click();
  await page.locator('[data-target="savings"]').click();
  await page.locator('#previewSavingsAllocation').click();
  await expect(page.locator('#allocationPreview')).toContainText('No allocation available this cycle');
  await expect(page.locator('#confirmSavingsAllocation')).toBeDisabled();
  const after = await page.evaluate(async () => ({state:await fetch('/api/savings/state').then(r=>r.json()), budget:await fetch('/api/budget/summary').then(r=>r.json()), plan:await fetch('/api/savings/allocation/preview').then(r=>r.json())}));
  expect(after.plan.remaining_available_cents).toBe(0);
  expect(after.state.goals.find(goal => goal.name === 'Unfunded protected Goal').funded_cents).toBe(0);
  expect(after.state.reserves.map(row => [row.destination_id,row.funded_cents])).toEqual(before.state.reserves.map(row => [row.destination_id,row.funded_cents]));
  expect(after.budget.account_state.checking_balance).toBe(before.budget.account_state.checking_balance);
  expect(after.budget.safe_to_spend.safe_to_spend).toBe(before.budget.safe_to_spend.safe_to_spend);
  expect(seen.mutations.filter(row => row.path === '/api/savings/allocation/apply')).toHaveLength(0);
  expect(seen.failures).toEqual([]); expect(seen.consoleErrors).toEqual([]);
});

test('Authenticated households remain isolated for Savings and Goals', async ({ page }) => {
  const seen = monitor(page);
  await login(page, {width:1440,height:1000});
  const a = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  const aGoal = a.goals.find(goal => goal.name === 'Slice 3 Goal Edited');
  const aReserve = a.reserves[0];
  expect(aGoal).toBeTruthy(); expect(aReserve).toBeTruthy();
  await loginAs(page, 'savings-browser-b@example.com', 'browser-pass-b');
  const b = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(b.goals.some(goal => goal.name === 'Slice 3 Goal Edited')).toBeFalsy();
  expect(b.reserves.some(reserve => reserve.destination_id === aReserve.destination_id)).toBeFalsy();
  const cross = await page.evaluate(async ({goalId, destinationId}) => {
    const goal = await fetch(`/api/goals/${goalId}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:'cross-household'})});
    const reserve = await fetch(`/api/savings/transfer`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({operation_id:'cross-household',source_destination_id:destinationId,amount:'1',transfer_type:'reserve_use',purpose:'emergency',confirm:true})});
    return {goal:goal.status, reserve:reserve.status};
  }, {goalId:aGoal.id, destinationId:aReserve.destination_id});
  expect(cross.goal).toBe(400); expect(cross.reserve).toBe(400);
  const bAfter = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(bAfter).toEqual(b);
  await loginAs(page, 'recap-browser@example.com', 'browser-pass-123');
  const aAfter = await page.evaluate(() => fetch('/api/savings/state').then(r=>r.json()));
  expect(aAfter.goals.find(goal => goal.id === aGoal.id).name).toBe('Slice 3 Goal Edited');
  expect(aAfter.reserves.find(reserve => reserve.destination_id === aReserve.destination_id).funded_cents).toBe(aReserve.funded_cents);
  expect(seen.failures.filter(row => row.status === 400 && ['/api/goals/'+aGoal.id, '/api/savings/transfer'].includes(row.path))).toHaveLength(2);
  expect(seen.consoleErrors.filter(message => !message.includes('status of 400'))).toEqual([]);
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
