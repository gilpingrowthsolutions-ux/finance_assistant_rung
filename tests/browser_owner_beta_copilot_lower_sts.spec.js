const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5051';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function api(page, path) { return page.evaluate(endpoint => fetch(endpoint).then(r => r.json()), path); }
test('served Copilot declines spending above forward-adjusted canonical Safe-to-Spend', async ({ page }) => {
  await page.goto(ROOT, {waitUntil:'networkidle'});
  const login = await page.evaluate(() => fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email:'feature5-rebalance@example.com',password:'browser-rebalance'})}).then(r => r.status));
  expect(login).toBe(200); await page.reload({waitUntil:'networkidle'});
  const setupLater = page.getByRole('button', {name:'Set up later'}); if (await setupLater.isVisible()) await setupLater.click();
  const summary = await api(page, '/api/budget/summary');
  expect(summary.safe_to_spend.safe_to_spend_cents).toBe(2000);
  await page.setViewportSize({width:390,height:844});
  await page.locator('[data-target="copilot"]').click();
  await page.locator('#copilotInput').fill('Can I spend $50 tonight?');
  await page.locator('#copilotSendBtn').click();
  const thread = page.locator('#copilotThread');
  await expect(thread).toContainText('$50.00');
  await expect(thread).toContainText('$20.00');
  await expect(thread).toContainText('No.');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(page.locator('#copilotInput')).toBeVisible();
  await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  const after = await api(page, '/api/budget/summary');
  expect(after.safe_to_spend.safe_to_spend_cents).toBe(2000);
});
