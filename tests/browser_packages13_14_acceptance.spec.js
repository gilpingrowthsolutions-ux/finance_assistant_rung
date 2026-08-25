const { test, expect } = require('@playwright/test');

async function login(page) {
  await page.goto('http://127.0.0.1:5051/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelector('#authDialog')?.open || typeof window.rungOpenTab === 'function');
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('savings-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await page.waitForLoadState('networkidle');
  }
}

test('Packages 13-14 canonical savings and goal workflows', async ({ page }) => {
  page.on('pageerror', error => console.error('PAGEERROR', error.message));
  const mutations = [];
  page.on('request', req => { const path = new URL(req.url()).pathname; if (req.method() !== 'GET' && (path.startsWith('/api/goals') || path.startsWith('/api/reserves') || path.startsWith('/api/savings') || path === '/api/copilot/stage' || path === '/api/copilot/apply')) mutations.push(path); });
  await login(page);
  await page.locator('[data-target="goals"]').click();
  await page.locator('#goals details summary').click();
  await page.locator('#goalName').fill('Family Vacation'); await page.locator('#goalTarget').fill('1000'); await page.locator('#goalDate').fill('2026-12-31');
  await page.locator('#goalForm button[type=submit]').click();
  await expect(page.locator('#goalCards')).toContainText('$0.00 of $1,000.00');
  expect(mutations.filter(x => x === '/api/goals')).toHaveLength(1);
  const first = await page.evaluate(() => fetch('/api/savings/state').then(r => r.json())); expect(first.goals).toHaveLength(1);

  const goalEditAnswers = ['1200', '1'];
  const goalEditDialog = dialog => dialog.accept(goalEditAnswers.shift());
  page.on('dialog', goalEditDialog);
  await page.locator('.goal-edit').click();
  await expect.poll(async () => (await page.evaluate(() => fetch('/api/savings/state').then(r => r.json()))).goals[0].target_cents).toBe(120000);
  page.off('dialog', goalEditDialog);
  await page.locator('.goal-status').click(); await expect(page.locator('#goalCards')).toContainText('paused');
  await page.locator('.goal-status').click(); await expect(page.locator('#goalCards')).toContainText('active');

  await page.locator('[data-target="savings"]').click();
  await page.locator('#savings details summary').click();
  await page.locator('#reserveName').fill('Vehicle Repair Reserve'); await page.locator('#reserveCategory').selectOption('vehicle'); await page.locator('#reserveTarget').fill('100');
  await page.locator('#reserveForm button[type=submit]').click(); await expect(page.locator('#reserveCards')).toContainText('$0.00 of $100.00');
  await page.locator('#previewSavingsAllocation').click(); await expect(page.locator('#allocationPreview')).toContainText('Vehicle Repair Reserve');
  page.once('dialog', dialog => dialog.accept()); await page.locator('#confirmSavingsAllocation').click();
  await expect(page.locator('#reserveCards')).toContainText('$100.00 of $100.00');
  let state = await page.evaluate(() => fetch('/api/savings/state').then(r => r.json())); expect(state.goals[0].funded_cents).toBe(15000); expect(state.goals[0].remaining_cents).toBe(105000);

  const reserveUseAnswers = ['truck transmission repair', '60', ''];
  const reserveUseDialog = dialog => dialog.type() === 'confirm' ? dialog.accept() : dialog.accept(reserveUseAnswers.shift());
  page.on('dialog', reserveUseDialog); await page.locator('.reserve-use').click(); page.off('dialog', reserveUseDialog);
  await expect(page.locator('#reserveCards')).toContainText('$40.00 of $100.00');
  state = await page.evaluate(() => fetch('/api/savings/state').then(r => r.json())); expect(state.reserves[0].allocation_eligible).toBe(true);
  expect(mutations.filter(x => x === '/api/savings/allocation/apply')).toHaveLength(1);
  expect(mutations.filter(x => x === '/api/reserves')).toHaveLength(1);
  expect(mutations.filter(x => x === '/api/savings/transfer')).toHaveLength(1);
  expect(mutations.filter(x => x.startsWith('/api/goals/'))).toHaveLength(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.locator('#savings details[open] summary').click(); await page.locator('#savings').evaluate(el => el.scrollIntoView());
  await page.screenshot({path:'/tmp/rung-packages13-14-desktop.png', animations:'disabled'});

  await page.locator('[data-target="copilot"]').click(); await page.locator('#copilotInput').fill('Add a new laptop goal for $900 by 2027-01-01'); await page.locator('#copilotSendBtn').click();
  await expect(page.locator('#copilotStageToolbar')).toBeVisible(); await expect(page.locator('#copilotStageDialog')).toBeVisible(); await expect(page.locator('[data-stage-section="goals_added"][data-stage-field="name"]')).toHaveValue('New Laptop');
  await page.locator('#copilotApplyStageBtn').click(); await expect.poll(async () => (await page.evaluate(() => fetch('/api/savings/state').then(r => r.json()))).goals.length).toBe(2);
  expect(mutations.filter(x => x === '/api/copilot/stage')).toHaveLength(1); expect(mutations.filter(x => x === '/api/copilot/apply')).toHaveLength(1);

  await page.setViewportSize({width:390,height:844}); await page.locator('#mobileMoreBtn').click(); await page.locator('.mobile-more-destination[data-destination="goals"]').click();
  await expect(page.locator('#goals')).toBeVisible(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.locator('#goals details[open] summary').click(); await page.locator('#goals').evaluate(el => el.scrollIntoView());
  await page.screenshot({path:'/tmp/rung-packages13-14-mobile.png', animations:'disabled'});
});
