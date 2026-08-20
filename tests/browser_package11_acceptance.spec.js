const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

test('Package 11 manual-first onboarding is truthful, canonical, and single-submit', async ({ page }) => {
  const onboardingPosts = [];
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/onboarding/complete')) {
      onboardingPosts.push(request.postDataJSON());
    }
  });

  await page.goto('http://127.0.0.1:5051/', { waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).toBeVisible();

  const fresh = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(fresh.defaults.checking_balance).toBeNull();
  expect(fresh.defaults.pay_period_days).toBe(0);
  expect(fresh.defaults.expected_paycheck).toBeNull();
  expect(fresh.readiness.complete).toBe(false);

  await page.locator('#onboardingHouseholdSize').fill('3');
  await page.locator('#onboardingFavoriteProteins').fill('chicken, salmon');
  await page.locator('#onboardingShoppingStyle').selectOption('save_most');
  await page.locator('#onboardingMilkType').selectOption('whole');
  await page.locator('#onboardingBreadType').selectOption('wheat');
  await page.locator('#onboardingLocationSharing').check();
  await page.locator('#onboardingNextBtn').click();

  const rows = page.locator('#onboardingBillsContainer .onboarding-bills');
  await expect(rows).toHaveCount(3);
  await rows.nth(0).locator('.onboarding-bill-amount').fill('95');
  await rows.nth(1).locator('.onboarding-bill-amount').fill('70');
  await rows.nth(2).locator('.onboarding-bill-amount').fill('140');
  await page.locator('#onboardingNextBtn').click();

  await page.locator('#onboardingBalance').fill('1750.50');
  await page.locator('#onboardingPayPeriod').fill('14');
  await page.locator('#onboardingExpectedPaycheck').fill('2000');
  await page.locator('#onboardingNextPayday').fill('2026-08-26');
  await page.locator('#onboardingPyfTarget').fill('12.5');
  await page.locator('#onboardingSafeBuffer').fill('150');
  await page.locator('#onboardingGroceryBaseline').fill('240');
  await page.locator('#onboardingFuelBaseline').fill('75');
  await page.locator('#onboardingNextBtn').click();

  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  expect(onboardingPosts).toHaveLength(1);

  const saved = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(saved.readiness.complete).toBe(true);
  expect(saved.defaults).toMatchObject({
    checking_balance: 1750.5,
    pay_period_days: 14,
    expected_paycheck: 2000,
    next_payday: '2026-08-26',
    long_term_savings_target_percent: 12.5,
    protected_buffer: 150,
    baseline_grocery_cost: 240,
    baseline_fuel_cost: 75,
    shopping_style: 'save_most',
    location_sharing_enabled: true,
  });
  expect(saved.defaults.household_shopping_defaults).toMatchObject({ milk_type: 'whole', bread_type: 'wheat' });

  const snapshot = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(snapshot.safe_to_spend.complete).toBe(true);
  expect(snapshot.safe_to_spend.components.grocery_commitment_total).toBe(240);
  expect(snapshot.safe_to_spend.freshness.bank_connected).toBe(false);

  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  const reloaded = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(reloaded.defaults).toMatchObject(saved.defaults);

  const revisit = await page.evaluate(async () => {
    const response = await fetch('/api/onboarding/complete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ household_size: 3 }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(revisit.status).toBe(200);
  const afterRevisit = await page.evaluate(async () => (await fetch('/api/onboarding/state')).json());
  expect(afterRevisit.defaults.shopping_style).toBe('save_most');
  expect(afterRevisit.defaults.household_shopping_defaults).toMatchObject({ milk_type: 'whole', bread_type: 'wheat' });
  expect(afterRevisit.defaults.checking_balance).toBe(1750.5);
});
