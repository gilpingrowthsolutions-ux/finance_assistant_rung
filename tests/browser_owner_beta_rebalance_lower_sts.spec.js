const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5051';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function api(page, method, path, body) {
  return page.evaluate(async (request) => { const response = await fetch(request.path, {
    method: request.method, headers: request.body ? {'Content-Type':'application/json'} : {},
    body: request.body ? JSON.stringify(request.body) : undefined,
  }); return { status: response.status, data: await response.json() }; }, {method, path, body});
}
test('served Rebalance caps stale state-X budget at forward-adjusted state-Y Safe-to-Spend', async ({ page }) => {
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  expect((await api(page, 'POST', '/api/auth/login', {email:'feature5-rebalance@example.com', password:'browser-rebalance'})).status).toBe(200);
  await page.reload({waitUntil:'networkidle'});
  const setupLater = page.getByRole('button', {name:'Set up later'});
  if (await setupLater.isVisible()) await setupLater.click();
  await page.locator('[data-target="shopping"]').click();
  const before = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  const summary = (await api(page, 'GET', '/api/budget/summary')).data.safe_to_spend;
  expect(summary.safe_to_spend_cents).toBe(2000);
  await page.locator('summary', {hasText:'Use a different budget for this trip'}).click();
  await page.locator('#budgetInput').fill('99');
  const previewWait = page.waitForResponse(response => new URL(response.url()).pathname === '/api/grocery/rebalance/preview');
  await page.locator('#rebalanceCartBtn').click();
  const previewResponse = await previewWait;
  expect(previewResponse.request().postDataJSON()).toMatchObject({budget_limit:99});
  const preview = await previewResponse.json();
  expect(preview.canonical_safe_to_spend_cents).toBe(2000);
  expect(preview.budget_cents).toBeLessThanOrEqual(2000);
  expect(preview.proposal.status).toBe('pending');
  expect(preview.proposal.changes).toEqual([expect.objectContaining({package_count:3, proposed_product_id:'RB-VALUE'})]);
  await expect(page.locator('#rebalanceReviewDialog')).toHaveAttribute('open', '');
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(page.locator('#rebalanceReviewDialog')).toContainText('Rebalance value detergent');
  await expect(page.locator('#rebalanceCancelBtn')).toBeVisible();
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart).toEqual(before);
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.name).toBe('Store A');
});
