const { test, expect } = require('playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

async function openSettings(page) {
  await page.goto('http://127.0.0.1:5051/#settings', { waitUntil: 'networkidle' });
  await page.locator('[data-target="settings"]').click();
  await expect(page.locator('#settings')).toBeVisible();
}

test('Package 12 real-browser functional acceptance', async ({ page }) => {
  const mutationRequests = [];
  const consoleErrors = [];
  const failedLocalRequests = [];
  page.on('request', request => {
    if (request.method() !== 'GET') mutationRequests.push(new URL(request.url()).pathname);
  });
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  page.on('response', response => {
    if (response.url().startsWith('http://127.0.0.1:5051/') && response.status() >= 400) failedLocalRequests.push(response.url());
  });
  await openSettings(page);

  await expect(page.locator('#settingsPyfTarget')).toHaveValue('18.5');
  await expect(page.locator('#settingsSafeBuffer')).toHaveValue('225.00');
  await expect(page.locator('#settingsPayPeriod')).toHaveValue('14');
  await expect(page.locator('#settingsExpectedPaycheck')).toHaveValue('2100.00');
  await page.locator('[data-settings-section="shopping"]').click();
  await expect(page.locator('#householdShoppingStyle')).toHaveValue('save_most');
  await expect(page.locator('#householdPref_milk_type')).toHaveValue('whole');
  await expect(page.locator('#householdPref_bread_type')).toHaveValue('wheat');
  await page.screenshot({ path: '/tmp/rung-package12-settings-shopping.png', fullPage: true });
  await page.locator('[data-settings-section="location"]').click();
  await expect(page.locator('#settingsLocationSharing')).not.toBeChecked();
  await expect(page.locator('#settingsCurrentLocation')).toContainText('ZIP: 65084');
  await expect(page.locator('#settingsCurrentLocation')).toContainText('Versailles, MO');
  await expect(page.locator('#settingsCurrentLocation')).toContainText('GPS: 38.4314, -92.8410');
  await expect(page.locator('#settingsCurrentLocation').locator('input, button, select')).toHaveCount(0);
  await expect(page.locator('#settingsStoreName')).toContainText('Walmart — Versailles');
  await page.screenshot({ path: '/tmp/rung-package12-settings-location.png', fullPage: true });

  mutationRequests.length = 0;
  await page.locator('#settingsLocationSharing').check();
  await expect.poll(() => mutationRequests.filter(p => p === '/api/settings/location-sharing').length).toBe(1);
  expect(mutationRequests.filter(p => p === '/api/settings/location-sharing')).toHaveLength(1);

  await page.locator('[data-settings-section="financial"]').click();
  await page.locator('#settingsPyfTarget').fill('23.5');
  await page.locator('#settingsSafeBuffer').fill('275.25');
  await page.locator('#updateRatiosBtn').click();
  await expect(page.locator('#settingsFinancialStatus')).toContainText('Saved.');
  mutationRequests.length = 0;
  await page.locator('#settingsExpectedPaycheck').fill('2200.00');
  await page.locator('#updateRatiosBtn').click();
  await expect.poll(() => mutationRequests.filter(p => p === '/api/account/update').length).toBe(1);
  const incomeAfterEdit = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(Number(incomeAfterEdit.account_state.expected_paycheck)).toBe(2100);
  expect(Number(incomeAfterEdit.account_state.next_expected_paycheck)).toBe(2200);
  await expect(page.locator('#settingsExpectedPaycheckStatus')).toContainText('Next payday: $2,200.00');
  await page.locator('[data-settings-section="shopping"]').click();
  await page.locator('#householdShoppingStyle').selectOption('prefer_brands_when_possible');
  await page.locator('#householdPref_milk_type').selectOption('two_percent');
  await page.locator('#saveHouseholdShoppingDefaultsBtn').click();
  await expect(page.locator('#householdShoppingDefaultsStatus')).toContainText('Loaded');

  const storeBefore = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
  const discovery = await page.evaluate(async () => {
    const response = await fetch('/api/location/nearby-stores', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zip_code: '65026' }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(discovery.status).toBe(200);
  const storeAfter = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
  expect(storeAfter.selected_store).toEqual(storeBefore.selected_store);

  const canonical = await page.evaluate(async () => {
    const [location, retailer, grocery] = await Promise.all([
      fetch('/api/settings/current-location').then(r => r.json()),
      fetch('/api/settings/grocery-retailer').then(r => r.json()),
      fetch('/api/grocery').then(r => r.json()),
    ]);
    return { location, retailer, grocery };
  });
  expect(canonical.location.selected_store.store_id).toBe('357');
  expect(canonical.retailer.canonical_store.store_id).toBe('357');
  expect(canonical.grocery.items[0].store_name).toBe('Walmart — Versailles');
  expect(JSON.stringify(canonical.grocery)).not.toContain('65026');

  await expect(page.locator('#settings').getByRole('button', { name: /find stores|select store|find stores near zip/i })).toHaveCount(0);
  await expect(page.locator('#settings').locator('#settingsZip, #settingsBalance, #usageDailyCeiling')).toHaveCount(0);
  await expect(page.locator('#settings')).not.toContainText(/api[_ -]?key|secret key|provider credential/i);
  await page.locator('[data-settings-section="notifications"]').click();
  await expect(page.locator('[data-settings-pane="notifications"]')).toContainText('not available yet');
  await page.screenshot({ path: '/tmp/rung-package12-settings-notifications.png', fullPage: true });
  await page.locator('[data-settings-section="account"]').click();
  await page.screenshot({ path: '/tmp/rung-package12-settings-account.png', fullPage: true });

  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-target="settings"]').click();
  await expect(page.locator('#settingsPyfTarget')).toHaveValue('23.5');
  await expect(page.locator('#settingsSafeBuffer')).toHaveValue('275.25');
  await page.locator('[data-settings-section="shopping"]').click();
  await expect(page.locator('#householdShoppingStyle')).toHaveValue('prefer_brands_when_possible');
  await expect(page.locator('#householdPref_milk_type')).toHaveValue('two_percent');
  await page.locator('[data-settings-section="location"]').click();
  await expect(page.locator('#settingsLocationSharing')).toBeChecked();
  await expect(page.locator('#settingsStoreName')).toContainText('Walmart — Versailles');
  expect(consoleErrors).toEqual([]);
  expect(failedLocalRequests).toEqual([]);
});

test('Package 12 desktop and mobile Settings layouts are usable', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openSettings(page);
  const desktop = await page.locator('#settings').boundingBox();
  expect(desktop.width).toBeGreaterThan(900);
  await expect(page.locator('.sidebar')).toBeVisible();
  await page.screenshot({ path: '/tmp/rung-package12-settings-desktop.png', fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { document.querySelector('.content').scrollTop = 0; });
  const viewport = page.viewportSize();
  const settings = await page.locator('#settings').boundingBox();
  const save = await page.locator('#updateRatiosBtn').boundingBox();
  expect(settings.x).toBeGreaterThanOrEqual(0);
  expect(settings.x + settings.width).toBeLessThanOrEqual(viewport.width + 1);
  expect(save.x).toBeGreaterThanOrEqual(0);
  expect(save.x + save.width).toBeLessThanOrEqual(viewport.width + 1);
  await page.locator('#mobileMoreBtn').click();
  await expect(page.locator('.mobile-more-destination', { hasText: 'Settings' })).toBeVisible();
  await page.locator('.mobile-more-destination', { hasText: 'Settings' }).click();
  await page.locator('#settingsPyfTarget').fill('24');
  await expect(page.locator('#settingsPyfTarget')).toHaveValue('24');
  await page.screenshot({ path: '/tmp/rung-package12-settings-mobile.png', fullPage: true });
});
