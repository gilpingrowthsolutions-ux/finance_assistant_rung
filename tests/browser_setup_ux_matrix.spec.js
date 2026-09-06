const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL;

async function login(page, email) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill(email);
  await page.locator('#authPassword').fill('sts-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
}
async function resume(page) {
  await page.locator('#overviewSetupBtn').click();
  await expect(page.locator('#onboardingDialog')).toBeVisible();
}
async function finish(page) {
  for (let index = 0; index < 4; index += 1) await page.locator('#onboardingNextBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
}
async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test.describe.serial('P1 setup UX remaining matrix', () => {
  test('payday-only resume is specific, preserves established values, and reloads', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'matrix-payday@example.com');
    await expect(page.locator('#overviewSetupText')).toContainText('Confirm your next payday');
    await expect(page.locator('#overviewSetupText')).not.toContainText('expected paycheck');
    await resume(page);
    await expect(page.locator('#onboardingNextPayday')).toBeVisible();
    await expect(page.locator('#onboardingNextPayday')).toBeFocused();
    await expect(page.locator('#onboardingBalance')).toBeHidden();
    await expect(page.locator('#onboardingExpectedPaycheck')).toBeHidden();
    await page.locator('#onboardingNextPayday').fill(new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10));
    await finish(page);
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await expect(page.locator('#safeHeroNextIncome')).not.toContainText('not set');
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await page.locator('[data-target="settings"]').click();
    await page.locator('[data-target="overview"]').click();
    await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await page.setViewportSize({ width: 390, height: 844 }); await noOverflow(page);
  });

  test('required-expense explicit-none is discoverable and creates no records', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'matrix-expenses@example.com');
    await expect(page.locator('#overviewSetupText')).toContainText('Review your required expenses');
    await resume(page);
    await expect(page.locator('#onboardingRequiredExpenses')).toBeVisible();
    await expect(page.locator('#onboardingBalance')).toBeHidden();
    await page.locator('input[name="onboardingExpenses"][value="no"]').check();
    await expect(page.locator('#onboardingNoExpensesStatus')).toBeVisible();
    await finish(page);
    expect(await page.evaluate(async () => (await fetch('/bills')).json())).toEqual([]);
    expect(await page.evaluate(async () => (await fetch('/api/transactions')).json())).toEqual([]);
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await page.setViewportSize({ width: 390, height: 844 }); await noOverflow(page);
  });

  test('grocery-only resume skips completed controls and persists', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'matrix-grocery@example.com');
    await expect(page.locator('#overviewSetupText')).toContainText('Review grocery costs');
    await resume(page);
    await expect(page.locator('#onboardingGroceryBaseline')).toBeVisible();
    await expect(page.locator('#onboardingGroceryBaseline')).toBeFocused();
    await expect(page.locator('#onboardingFuelBaseline')).toBeHidden();
    await expect(page.locator('#onboardingBalance')).toBeHidden();
    await page.locator('#onboardingGroceryBaseline').fill('100'); await finish(page);
    await page.reload({ waitUntil: 'networkidle' }); await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await page.setViewportSize({ width: 390, height: 844 }); await noOverflow(page);
  });

  test('transport-only resume skips completed controls and persists', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'matrix-transport@example.com');
    await expect(page.locator('#overviewSetupText')).toContainText('Review transportation costs');
    await resume(page);
    await expect(page.locator('#onboardingFuelBaseline')).toBeVisible();
    await expect(page.locator('#onboardingFuelBaseline')).toBeFocused();
    await expect(page.locator('#onboardingGroceryBaseline')).toBeHidden();
    await page.locator('#onboardingFuelBaseline').fill('50'); await finish(page);
    await page.reload({ waitUntil: 'networkidle' }); await expect(page.locator('#overviewSetupNotice')).not.toHaveClass(/is-visible/);
    await page.setViewportSize({ width: 390, height: 844 }); await noOverflow(page);
  });

  test('multiple missing items use canonical order and Overview has one balance action', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page, 'matrix-multi@example.com');
    await expect(page.locator('#overviewSetupText')).toContainText('2 things left: Review grocery costs • Review transportation costs.');
    await resume(page);
    await expect(page.locator('#onboardingGroceryBaseline')).toBeFocused();
    await expect(page.locator('#onboardingFuelBaseline')).toBeVisible();
    await expect(page.locator('#onboardingBalance')).toBeHidden();
    await page.locator('#onboardingSkipAllBtn').click();
    await expect(page.locator('#overviewCheckingBalance')).toBeVisible();
    await expect(page.locator('#overviewUpdateBalanceBtn')).toHaveCount(0);
    await expect(page.locator('#overviewBalanceCard')).toHaveCount(0);
    await page.locator('[data-target="transactions"]').click();
    await expect(page.locator('#moneyUpdateBalanceBtn')).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 }); await page.locator('[data-target="overview"]').click(); await noOverflow(page);
  });
});
