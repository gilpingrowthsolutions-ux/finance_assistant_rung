const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL;
test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });

async function settings(page) {
  await page.goto(ROOT + '/#settings', { waitUntil: 'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('location-browser@example.com');
    await page.locator('#authPassword').fill('location-pass-123');
    await page.locator('#authLoginBtn').click();
  }
  if (await page.locator('[data-target="settings"]').isVisible()) await page.locator('[data-target="settings"]').click();
  await page.locator('[data-settings-section="location"]').click();
}
async function location(page) { return page.evaluate(() => fetch('/api/settings/current-location').then(r => r.json())); }

test('live device success is current while selected store stays fixed', async ({ page, context }) => {
  await context.grantPermissions(['geolocation'], { origin: ROOT });
  await context.setGeolocation({ latitude: 37.7749, longitude: -122.4194 });
  await settings(page);
  await expect(page.locator('#settingsCurrentLocation')).toContainText('Current location');
  await expect(page.locator('#settingsCurrentLocation')).toContainText('94105');
  await expect(page.locator('#settingsCurrentLocation')).not.toContainText('65084');
  expect((await location(page)).selected_store.store_id).toBe('61500116');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test('permission denial is truthful and preserves store on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.evaluate(() => fetch('/api/location/current-device', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ outcome: 'unavailable' }) }));
  await settings(page);
  await expect(page.locator('#settingsCurrentLocation')).toContainText('Location unavailable');
  await expect(page.locator('#settingsCurrentLocation')).toContainText('Last known location');
  await expect(page.locator('#settingsCurrentLocation')).not.toContainText('Current location');
  expect((await location(page)).selected_store.store_id).toBe('61500116');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test('sharing off requests no device location and preserves selected store on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await settings(page);
  await page.evaluate(() => fetch('/api/settings/location-sharing', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ location_sharing_enabled: false }) }));
  const currentDevicePosts = [];
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/location/current-device') currentDevicePosts.push(request.url());
  });
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-settings-section="location"]').click();
  await expect(page.locator('#settingsLocationSharing')).not.toBeChecked();
  await expect(page.locator('#settingsCurrentLocation')).toContainText('Location sharing is off');
  await expect(page.locator('#settingsCurrentLocation')).not.toContainText('Current location');
  expect(currentDevicePosts).toEqual([]);
  expect((await location(page)).selected_store.store_id).toBe('61500116');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
