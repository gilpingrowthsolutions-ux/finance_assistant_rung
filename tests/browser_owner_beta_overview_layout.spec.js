// Requires a fresh disposable RUNG_DB_PATH with RUNG_ENV=beta, seeded by
// seed_owner_beta_overview_layout.py.
const { test, expect } = require('playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL;
if (process.env.RUNG_CHROMIUM_PATH) test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });

async function login(page) {
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('owner-beta-overview@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  await expect(page.locator('#safeHeroAmount')).toHaveText('$1,050.00');
}

test('owner-approved Overview grid and neutral Safe-to-Spend frame', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page);
  await expect(page.locator('#kpiBalance')).toHaveText('$1,250.00');
  await expect(page.locator('#allocUnpaidAmt')).toHaveText('$0.00');
  await expect(page.locator('#allocPyfAmt')).toHaveText('$120.00');
  await expect(page.locator('#allocBufferAmt')).toHaveText('$80.00');
  await expect(page.locator('#safeHeroUntil')).toHaveText('Until payday: 11 days');
  await expect(page.locator('#safeHeroNextIncome')).toContainText('$1,200.00');

  const geometry = await page.evaluate(() => {
    const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
    const safe = rect('.overview-hero');
    const protectedMoney = rect('.protection-card');
    const cycle = rect('#overviewTrajectory');
    const recap = rect('#overviewPaydayRecap');
    const arc = rect('.safe-gauge-arc');
    const label = rect('#safeHeroState');
    const amount = rect('#safeHeroAmount');
    return {
      safe, protectedMoney, cycle, recap, arc, label, amount,
      contentCopilotCount: document.querySelectorAll('#overview #overviewAskCopilotBtn').length,
      lowerCopilotCount: document.querySelectorAll('#overview .copilot-entry').length,
      balanceActions: document.querySelectorAll('#overview #overviewCheckingBalance').length,
      greenArc: getComputedStyle(document.querySelector('.safe-gauge-arc')).borderTopColor,
    };
  });
  expect(Math.abs(geometry.safe.left - geometry.cycle.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.safe.width - geometry.cycle.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.protectedMoney.left - geometry.recap.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.protectedMoney.width - geometry.recap.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.cycle.top - geometry.recap.top)).toBeLessThanOrEqual(1);
  expect(geometry.amount.top).toBeGreaterThanOrEqual(geometry.arc.bottom);
  expect(geometry.label.top).toBeGreaterThanOrEqual(geometry.arc.bottom);
  expect(geometry.contentCopilotCount).toBe(1);
  expect(geometry.lowerCopilotCount).toBe(0);
  expect(geometry.balanceActions).toBe(1);
  expect(geometry.greenArc).toBe('rgb(229, 235, 231)');
  await page.screenshot({ path: '/tmp/rung-owner-beta-overview-desktop.png', fullPage: true, animations: 'disabled' });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  const order = await page.locator('.overview-grid > article').evaluateAll((cards) => cards.map((card) => card.id || card.className));
  expect(order.slice(0, 4)).toEqual(['overview-card overview-hero', 'overview-card protection-card', 'overviewTrajectory', 'overviewPaydayRecap']);
  await page.screenshot({ path: '/tmp/rung-owner-beta-overview-mobile.png', fullPage: true, animations: 'disabled' });
  await page.locator('#overviewPaydayRecap').scrollIntoViewIfNeeded();
  await expect(page.locator('#overviewPaydayRecap')).toBeInViewport();
  await page.screenshot({ path: '/tmp/rung-owner-beta-overview-mobile-lower.png', animations: 'disabled' });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('#overviewCheckingBalance').click();
  await page.locator('#overviewBalanceInput').fill('200.00');
  await page.locator('#overviewBalanceSave').click();
  await expect(page.locator('#safeHeroAmount')).toHaveText('$0.00');
  expect(await page.locator('#safeHeroAmount').boundingBox()).not.toBeNull();
  expect(await page.locator('.safe-gauge-arc').evaluate((arc) => getComputedStyle(arc).borderTopColor)).toBe('rgb(229, 235, 231)');
});
