const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL;
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function login(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('owner-beta-timeline@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
}
test('mobile Overview keeps factual cycle information and quiet details usable', async ({ page }) => {
  await login(page);
  await expect(page.locator('#safeHeroAmount')).toHaveText('$1,260.57');
  await expect(page.locator('#overviewCycleDates')).toBeVisible();
  await expect(page.locator('#safeHeroNextIncome')).toContainText('Next expected income');
  await expect(page.locator('#overviewTrajectoryValue')).toHaveText('Cycle facts');
  await expect(page.locator('#overviewTrajectoryReason')).not.toContainText(/ahead|behind/i);
  await page.locator('.overview-quiet-details summary').click();
  await expect(page.locator('#reconciliationList')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(page.locator('[data-target="transactions"]')).toBeVisible();
});
