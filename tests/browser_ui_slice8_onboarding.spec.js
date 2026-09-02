const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

const baseURL = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5068';

async function fillRequiredSetup(page) {
  await page.locator('#onboardingBalance').fill('1750.50');
  await page.locator('#onboardingPayPeriod').fill('14');
  await page.locator('#onboardingNextPayday').fill('2026-09-04');
  await page.locator('#onboardingExpectedPaycheck').fill('2000');
  await page.locator('#onboardingPyfTarget').fill('12.5');
  await page.locator('#onboardingSafeBuffer').fill('150');
}

test('Slice 8 required-first no-expense path is real, responsive, and ready', async ({ page }) => {
  const requests = [];
  const consoleErrors = [];
  page.on('request', request => { if (request.method() === 'POST') requests.push(request.url()); });
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(baseURL + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).toBeVisible();
  await expect(page.locator('#onboardingStepTitle')).toHaveText('Set up the basics');
  expect(await page.locator('[data-step="0"] .onboarding-required').count()).toBeGreaterThanOrEqual(7);
  await page.screenshot({ path: '/tmp/rung_slice8_required_desktop.png', animations: 'disabled' });

  await fillRequiredSetup(page);
  await page.locator('input[name="onboardingExpenses"][value="no"]').check();
  await expect(page.locator('#onboardingNoExpensesStatus')).toBeVisible();
  await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('[data-step="1"]')).toBeVisible();
  await page.locator('#onboardingSkipStepBtn').click();
  await expect(page.locator('[data-step="2"]')).toBeVisible();
  const storeBefore = await page.evaluate(async () => (await fetch('/api/settings/grocery-retailer')).json());
  await page.locator('#onboardingLocationSharing').check();
  await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('[data-step="3"]')).toBeVisible();
  await expect(page.locator('#onboardingReviewList')).toContainText('Reviewed — none');
  await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();

  // Completion returns to the real Overview and re-reads the canonical
  // financial snapshot; a user must never need a hard reload for STS.
  await expect(page.locator('#overview')).toBeVisible();
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('—');

  const state = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(state.required_expense_review).toBe('no_expenses_reviewed');
  expect(state.readiness.complete).toBe(true);
  expect(requests.filter(url => url.endsWith('/api/onboarding/required-expenses-review'))).toHaveLength(1);
  expect(requests.filter(url => url.endsWith('/api/onboarding/complete'))).toHaveLength(1);
  expect(consoleErrors).toEqual([]);

  // Enabling Location Sharing during onboarding must never itself select or
  // change the shopping store: only Shopping/Copilot store selection may.
  const storeAfter = await page.evaluate(async () => (await fetch('/api/settings/grocery-retailer')).json());
  expect(storeAfter).toEqual(storeBefore);
  expect(Boolean((storeAfter.canonical_store || {}).canonical)).toBe(false);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(baseURL + '/', { waitUntil: 'networkidle' });
  await page.screenshot({ path: '/tmp/rung_slice8_completed_mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('Slice 8 YES review stays on Page 1, writes one real bill, then becomes ready', async ({ page }) => {
  const requests = [];
  page.on('request', request => { if (request.method() === 'POST') requests.push(request.url()); });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(baseURL + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).toBeVisible();
  await page.screenshot({ path: '/tmp/rung_slice8_required_mobile.png', animations: 'disabled' });
  await fillRequiredSetup(page);
  await page.locator('input[name="onboardingExpenses"][value="yes"]').check();
  await expect(page.locator('#onboardingExpenseReview')).toBeVisible();
  await page.locator('#onboardingGroceryBaseline').fill('240');
  await page.locator('#onboardingFuelBaseline').fill('75');
  await page.locator('#onboardingAddBillRowBtn').click();
  const row = page.locator('#onboardingBillsContainer .onboarding-bills').first();
  await row.locator('.onboarding-bill-name').fill('Internet');
  await row.locator('.onboarding-bill-amount').fill('70');
  await page.screenshot({ path: '/tmp/rung_slice8_yes_expense_mobile.png', animations: 'disabled' });
  await page.locator('#onboardingFinishExpenseReviewBtn').click();
  await page.locator('#onboardingNextBtn').click();
  await page.locator('#onboardingSkipStepBtn').click();
  await page.locator('#onboardingSkipStepBtn').click();
  await expect(page.locator('#onboardingReviewList')).toContainText('1 bill(s) reviewed');
  await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  const state = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(state.required_expense_review).toBe('has_expenses_reviewed');
  expect(state.readiness.complete).toBe(true);
  expect(requests.filter(url => url.endsWith('/api/onboarding/required-expenses-review'))).toHaveLength(2);
  expect(requests.filter(url => url.endsWith('/api/onboarding/complete'))).toHaveLength(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('Slice 8 correcting YES to NO does not leave stale bills or baselines behind', async ({ page }) => {
  await page.goto(baseURL + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).toBeVisible();
  await fillRequiredSetup(page);
  await page.locator('input[name="onboardingExpenses"][value="yes"]').check();
  await expect(page.locator('#onboardingExpenseReview')).toBeVisible();
  await page.locator('#onboardingGroceryBaseline').fill('240');
  await page.locator('#onboardingFuelBaseline').fill('75');
  await page.locator('#onboardingAddBillRowBtn').click();
  const row = page.locator('#onboardingBillsContainer .onboarding-bills').first();
  await row.locator('.onboarding-bill-name').fill('Internet');
  await row.locator('.onboarding-bill-amount').fill('70');

  // The person reconsiders before finishing the YES review and corrects to NO.
  await page.locator('#onboardingCorrectExpensesBtn').click();
  await expect(page.locator('#onboardingNoExpensesStatus')).toBeVisible();
  await expect(page.locator('#onboardingExpenseReview')).not.toBeVisible();
  expect(await page.locator('#onboardingBillsContainer .onboarding-bills').count()).toBe(0);
  expect(await page.locator('#onboardingGroceryBaseline').inputValue()).toBe('');
  expect(await page.locator('#onboardingFuelBaseline').inputValue()).toBe('');

  await page.locator('#onboardingNextBtn').click();
  await page.locator('#onboardingSkipStepBtn').click();
  await page.locator('#onboardingSkipStepBtn').click();
  await expect(page.locator('#onboardingReviewList')).toContainText('Reviewed — none');
  await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();

  const state = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(state.required_expense_review).toBe('no_expenses_reviewed');
  expect(state.readiness.complete).toBe(true);
  expect(state.bill_templates.every(b => b.amount == null)).toBe(true);
  expect(state.defaults.baseline_grocery_cost).toBeNull();
  expect(state.defaults.baseline_fuel_cost).toBeNull();
});

test('Slice 8 Set up later dismisses onboarding without manufacturing readiness', async ({ page }) => {
  await page.goto(baseURL + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).toBeVisible();
  await page.locator('#onboardingSkipAllBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  const state = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(state.is_onboarded).toBe(true);
  expect(state.required_expense_review).toBe('unanswered');
  expect(state.readiness.complete).toBe(false);
  await page.screenshot({ path: '/tmp/rung_slice8_setup_needed.png', animations: 'disabled' });
});
