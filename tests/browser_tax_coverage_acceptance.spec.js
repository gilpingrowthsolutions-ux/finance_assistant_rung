const { test, expect } = require('@playwright/test');
const ROOT = 'http://127.0.0.1:5056';
if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });

async function login(page) {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelector('#authDialog')?.open || typeof window.rungOpenTab === 'function');
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('tax-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await page.waitForLoadState('networkidle');
  }
}

async function checkPurchase(page, values) {
  await page.evaluate(() => window.rungOpenTab('canibuy'));
  await page.locator('#ciItem').fill(values.item || 'Laundry detergent');
  await page.locator('#ciPrice').fill(String(values.price || '10.00'));
  await page.locator('#ciContext').selectOption(values.context || 'selected_physical_store');
  await page.locator('#ciCategory').selectOption(values.category || 'general_merchandise');
  if (values.context && values.context !== 'selected_physical_store') {
    await page.locator('#ciCity').fill(values.city || '');
    await page.locator('#ciState').fill(values.state || '');
    await page.locator('#ciPostal').fill(values.postal || '');
  }
  const responsePromise = page.waitForResponse(r => r.url().includes('/api/decision/can-i-buy') && r.request().method() === 'POST');
  await page.locator('#canIBuyForm button[type=submit]').click();
  const response = await responsePromise;
  const body = await response.json();
  await expect(page.locator('#canIBuyResult')).toBeVisible();
  await expect(page.locator('#canIBuyResult')).toContainText(body.tax.label);
  return { text: await page.locator('#canIBuyResult').innerText(), body };
}

test('tax coverage confidence is truthful, store-controlled, and responsive', async ({ page }) => {
  test.setTimeout(90000);
  const errors = [], failed = [], mutations = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  page.on('requestfailed', r => failed.push(r.url()));
  page.on('request', r => { if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method())) mutations.push({ method: r.method(), path: new URL(r.url()).pathname }); });
  await login(page);

  mutations.length = 0;
  const before = await page.evaluate(async () => ({
    budget: await fetch('/api/budget/summary').then(r => r.json()),
    transactions: await fetch('/api/transactions').then(r => r.json()),
    store: await fetch('/api/settings/current-location').then(r => r.json()),
  }));
  expect(before.store.selected_store.store_id).toBe('357');

  let check = await checkPurchase(page, { context: 'selected_physical_store' });
  let text = check.text;
  expect(text).toContain('Rung-calculated');
  expect(text).toContain('Tax-inclusive purchase');
  await page.screenshot({ path: '/tmp/rung-tax-supported-desktop.png', animations: 'disabled', fullPage: true });

  check = await checkPurchase(page, { context: 'manual_local', city: 'Columbia', state: 'MO', postal: '65201' }); text = check.text;
  expect(text).toContain('Estimated');
  let store = await page.evaluate(() => fetch('/api/settings/current-location').then(r => r.json()));
  expect(store.selected_store.store_id).toBe('357');

  check = await checkPurchase(page, { context: 'manual_local', city: 'Juneau', state: 'AK', postal: '99801' }); text = check.text;
  expect(text).toContain('Tax not included yet');
  expect(text).toContain('Pre-tax price');

  const actual = await page.evaluate(async () => {
    const r = await fetch('/api/decision/can-i-buy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item_name: 'Checkout item', cost: 10, purchase_context: 'manual_local', tax_category: 'unknown', state: 'AK', postal_code: '99801', actual_tax: 0.83 }) });
    return r.json();
  });
  expect(actual.tax.label).toBe('Confirmed');
  expect(actual.purchase_total).toBe(10.83);

  await page.route('**/api/location/nearby-stores', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', location: { zip_code: '65201', city_state: 'Columbia, MO' }, stores: [{ retailer: 'walmart', retailer_display: 'Walmart', store_id: 'columbia-1', name: 'Walmart — Columbia', address: '300 Local Ave, Columbia, MO 65201', postal_code: '65201', distance_miles: 1.2 }] }) }));
  await page.evaluate(() => window.rungOpenTab('settings'));
  await page.locator('#settings details').filter({ hasText: 'Advanced & Internal Controls' }).locator('summary').click();
  await page.locator('#settingsZip').fill('65201');
  await page.locator('#updateLocationBtn').click();
  await expect(page.locator('#settingsNearbyStores').getByRole('button', { name: 'Select Store' })).toBeVisible();
  await page.locator('#settingsNearbyStores').getByRole('button', { name: 'Select Store' }).click();
  await expect(page.locator('#settingsStoreName')).toContainText('Walmart — Columbia');
  await page.unroute('**/api/location/nearby-stores');
  check = await checkPurchase(page, { context: 'selected_physical_store' }); text = check.text;
  expect(text).toContain('Estimated');

  const after = await page.evaluate(async () => ({
    budget: await fetch('/api/budget/summary').then(r => r.json()),
    transactions: await fetch('/api/transactions').then(r => r.json()),
  }));
  expect(after.transactions).toEqual(before.transactions);
  expect(after.budget.safe_to_spend).toEqual(before.budget.safe_to_spend);
  expect(mutations.filter(x => x.path === '/api/location/select-store')).toHaveLength(1);
  expect(mutations.filter(x => ['/api/transactions', '/bills', '/api/savings/state'].includes(x.path))).toHaveLength(0);

  await page.reload({ waitUntil: 'networkidle' });
  store = await page.evaluate(() => fetch('/api/settings/current-location').then(r => r.json()));
  expect(store.selected_store.store_id).toBe('columbia-1');
  await page.setViewportSize({ width: 390, height: 844 });
  check = await checkPurchase(page, { context: 'online_delivery', city: 'Versailles', state: 'MO', postal: '65084' }); text = check.text;
  expect(text).toContain('Estimated');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/rung-tax-mobile.png', animations: 'disabled', fullPage: true });
  expect(failed).toEqual([]);
  expect(errors).toEqual([]);
});
