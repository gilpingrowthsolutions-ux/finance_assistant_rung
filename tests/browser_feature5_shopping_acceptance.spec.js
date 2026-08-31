const { test, expect } = require('playwright/test');

test.setTimeout(45000);
if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });

const ROOT = process.env.RUNG_BROWSER_ROOT || 'http://127.0.0.1:5051';
const A = { email: 'feature5-a@example.com', password: 'browser-pass-a' };
const B = { email: 'feature5-b@example.com', password: 'browser-pass-b' };

async function api(page, method, path, body) {
  return page.evaluate(async ({ method, path, body }) => {
    const response = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, data: await response.json().catch(() => ({})) };
  }, { method, path, body });
}

async function login(page, user) {
  await page.goto(ROOT, { waitUntil: 'domcontentloaded' });
  expect((await api(page, 'POST', '/api/auth/login', user)).status).toBe(200);
  await page.reload({ waitUntil: 'domcontentloaded' });
  const later = page.getByRole('button', { name: 'Set up later' });
  await later.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
  if (await later.isVisible()) await later.click();
  await expect(page.locator('#onboardingDialog')).not.toHaveAttribute('open', '', { timeout: 5000 });
}

function selectedLine(page) {
  return page.locator('[data-cart-current-line-key="manual:requirement:1"], [data-cart-current-line]').first();
}

async function buildCart(page) {
  const generated = page.waitForResponse(response => new URL(response.url()).pathname === '/api/grocery/generate-pay-period-plan' && response.request().method() === 'POST');
  await page.locator('#buildCartBtn').click();
  expect((await generated).status()).toBe(200);
  return (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
}

async function discoverStoresWithDeviceLocation(page) {
  await page.locator('#shoppingChangeStoreBtn').click();
  const discovery = page.waitForResponse(response => new URL(response.url()).pathname === '/api/location/nearby-stores');
  await page.locator('#shoppingUseLocationBtn').click();
  const response = await discovery;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toMatchObject({ auto_detect: true, latitude: 38.0, longitude: -92.0 });
  await expect(page.locator('#shoppingNearbyStores')).toContainText('Store A');
  await expect(page.locator('#shoppingNearbyStores')).toContainText('Store B');
  return response;
}

async function chooseStoreThroughUi(page, storeName, expectsReview = false) {
  await discoverStoresWithDeviceLocation(page);
  const row = page.locator('#shoppingNearbyStores .store-choice-row').filter({ hasText: storeName });
  await expect(row).toBeVisible();
  const selected = page.waitForResponse(response => new URL(response.url()).pathname === '/api/location/select-store');
  const staged = expectsReview ? page.waitForResponse(response => new URL(response.url()).pathname === '/api/shopping/store-change/start') : null;
  await row.getByRole('button', { name: 'Select Store' }).click();
  const selectResponse = await selected;
  return { selectResponse, stagedResponse: staged ? await staged : null };
}

test('Feature 5 served Shopping authority, location discovery, review, and household isolation', async ({ page }) => {
  const errors = [], failed = [], requests = [], chooseResponses = [], intentionalDenials = [];
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  page.on('requestfailed', request => failed.push(request.url()));
  page.on('request', request => requests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (path === '/api/shopping/current-cart/choose-product') chooseResponses.push(response.status());
    if ((path === '/api/location/select-store' || path.includes('/api/shopping/store-change/')) && [404, 409].includes(response.status())) {
      intentionalDenials.push({ method: response.request().method(), path, status: response.status() });
    }
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.context().grantPermissions(['geolocation'], { origin: ROOT });
  await page.context().setGeolocation({ latitude: 38.0, longitude: -92.0 });
  await login(page, B);
  expect((await api(page, 'POST', '/api/settings/location-sharing', { location_sharing_enabled: true })).status).toBe(200);
  await page.locator('[data-target="shopping"]').click();
  const bDiscoveryRequestStart = requests.length;
  await discoverStoresWithDeviceLocation(page);
  expect(requests.slice(bDiscoveryRequestStart)).toContain('POST /api/location/nearby-stores');
  expect(requests.slice(bDiscoveryRequestStart)).not.toContain('POST /api/location/select-store');
  expect(requests.slice(bDiscoveryRequestStart)).not.toContain('POST /api/shopping/store-change/start');
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.store_id).toBeFalsy();
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart).toBeNull();
  await expect(page.locator('#shoppingStoreName')).toHaveText('No store selected');
  await page.locator('#shoppingStoreCloseBtn').click();
  await page.locator('#buildCartBtn').click();
  await expect(page.locator('#storeCartContainer')).toContainText('Choose your shopping store first');
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart).toBeNull();

  await api(page, 'POST', '/api/auth/logout');
  await login(page, A);
  expect((await api(page, 'POST', '/api/settings/location-sharing', { location_sharing_enabled: true })).status).toBe(200);
  await page.locator('[data-target="shopping"]').click();
  const storeAResponse = await chooseStoreThroughUi(page, 'Store A');
  expect(storeAResponse.selectResponse.status()).toBe(200);
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart).toBeNull();
  await expect(page.locator('#shoppingStoreName')).toHaveText('Store A');
  let cart = await buildCart(page);
  await expect(selectedLine(page).locator('.cart-current-title')).toHaveText('Store A detergent');
  expect(cart.lines[0]).toMatchObject({ product_id: 'A-SKU', package_count: 3, unit_price: 8, line_total: 24 });
  expect(cart.lines).toHaveLength(2);

  await page.locator('summary', { hasText: 'More options' }).click();
  const alt = page.locator('.cart-product-choice').filter({ hasText: 'Store A alternate' });
  await expect(alt).toHaveCount(1);
  await alt.click();
  await expect(selectedLine(page).locator('.cart-current-title')).toHaveText('Store A alternate');
  await expect(selectedLine(page).locator('.cart-current-price')).toHaveText('$36.00');
  await expect.poll(() => chooseResponses.length).toBe(1);
  expect(chooseResponses).toEqual([200]);
  expect(requests.filter(request => request === 'POST /api/shopping/current-cart/choose-product')).toHaveLength(1);

  // A block is negative preference authority, not cart-rebuild authority.
  // Capture every mutation from the one real More Options click.
  const cartBeforeBlock = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  const blockClickStart = requests.length;
  const blockRequest = page.waitForResponse(response => new URL(response.url()).pathname === '/api/retail/product-block' && response.request().method() === 'POST');
  await selectedLine(page).getByRole('button', { name: /Do not automatically select Store A alternate/ }).click();
  expect((await blockRequest).status()).toBe(200);
  await page.waitForTimeout(150);
  const blockClickMutations = requests.slice(blockClickStart).filter(request => /^(POST|PUT|PATCH|DELETE) /.test(request));
  expect(blockClickMutations).toEqual(['POST /api/retail/product-block']);
  const blocksA = await api(page, 'GET', '/api/retail/product-block');
  expect(blocksA.status).toBe(200);
  expect(blocksA.data.blocks).toEqual(expect.arrayContaining([expect.objectContaining({ product_id: 'A-ALT' })]));
  const afterBlock = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  expect(afterBlock).toMatchObject({ id: cartBeforeBlock.id, version: cartBeforeBlock.version, total: cartBeforeBlock.total });
  expect(afterBlock.lines[0].product_id).toBe(cartBeforeBlock.lines[0].product_id);
  expect((await api(page, 'GET', '/api/retail/product-preference?base_item=laundry%20detergent&retailer=walmart')).data.preference).toBeNull();

  const beforeReload = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  expect(beforeReload.id).toBe(cart.id);
  expect(beforeReload.lines[0]).toMatchObject({ product_id: 'A-ALT', package_count: 3, unit_price: 12, line_total: 36 });

  // Device discovery refreshes candidates only.  With an exact selected store
  // and a user-selected persisted line, it must not create a review or mutate
  // either Shopping or Copilot authority.
  const locationBefore = (await api(page, 'GET', '/api/settings/current-location')).data.selected_store;
  const locationCartBefore = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  const locationRequestStart = requests.length;
  await discoverStoresWithDeviceLocation(page);
  await page.locator('#shoppingStoreCloseBtn').click();
  const locationCartAfter = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  const locationAfter = (await api(page, 'GET', '/api/settings/current-location')).data.selected_store;
  expect(locationAfter).toMatchObject({ store_id: locationBefore.store_id, name: locationBefore.name, retailer: locationBefore.retailer });
  expect(locationCartAfter).toMatchObject({ id: locationCartBefore.id, version: locationCartBefore.version, total: locationCartBefore.total });
  expect(locationCartAfter.lines[0]).toMatchObject({ product_id: locationCartBefore.lines[0].product_id });
  expect(requests.slice(locationRequestStart).filter(request => request === 'POST /api/shopping/store-change/start')).toEqual([]);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-target="copilot"]').click();
  await expect(page.locator('#copilotStoreTitle')).toHaveText('Store A');
  await page.locator('[data-target="shopping"]').click();

  const reloadStart = requests.length;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-target="shopping"]').click();
  const automaticMutations = requests.slice(reloadStart).filter(request => request.startsWith('POST ') || request.startsWith('PUT ') || request.startsWith('PATCH ') || request.startsWith('DELETE '));
  expect(automaticMutations).toEqual([]);
  const afterReload = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  expect(afterReload).toMatchObject({ id: beforeReload.id, version: beforeReload.version });
  expect(afterReload.lines[0]).toMatchObject({ product_id: 'A-ALT', package_count: 3, unit_price: 12, line_total: 36 });
  await expect(selectedLine(page).locator('.cart-current-title')).toHaveText('Store A alternate');
  await expect(selectedLine(page).locator('.cart-current-price')).toHaveText('$36.00');
  await expect(selectedLine(page).locator('.cart-current-package')).toHaveText('64 loads');
  await expect(selectedLine(page).locator('.cart-current-count')).toHaveText('3 packages');
  await expect(selectedLine(page).locator('.cart-current-requirement')).toHaveText('Laundry detergent');
  await expect(selectedLine(page).locator('.cart-persisted-save-preference')).toBeVisible();
  await expect(selectedLine(page).locator('.cart-current-title')).not.toHaveText('Store A detergent');
  await selectedLine(page).locator('summary', { hasText: 'More options' }).click();
  await expect(selectedLine(page).locator('.cart-persisted-product-choice')).toContainText('Alternative');
  await expect(selectedLine(page).locator('.cart-persisted-product-choice')).toContainText('Store A detergent');
  await expect(selectedLine(page).locator('.cart-persisted-block-product')).toBeVisible();
  await page.screenshot({ path: '/tmp/rung-feature5-authoritative-alt-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/rung-feature5-authoritative-alt-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Build Cart is an explicit re-resolution action. Its response, not a
  // stale pre-build DOM state, is what the user then sees.
  const rebuilt = await buildCart(page);
  await expect(selectedLine(page).locator('.cart-current-title')).toHaveText(rebuilt.lines[0].title);
  await expect(selectedLine(page).locator('.cart-current-price')).toHaveText('$' + rebuilt.lines[0].line_total.toFixed(2));

  // Store changes are a server-staged review: this direct served request is
  // the same endpoint used by the Shopping store-review flow.  It must not
  // mutate the canonical store/cart until explicit approval.
  const selectedA = (await api(page, 'GET', '/api/settings/current-location')).data.selected_store;
  const currentA = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  const beforeReviewBudget = (await api(page, 'GET', '/api/budget/summary')).data;
  const storeBReview = await chooseStoreThroughUi(page, 'Store B', true);
  expect(storeBReview.selectResponse.status()).toBe(409);
  expect(storeBReview.stagedResponse.status()).toBe(200);
  expect(storeBReview.stagedResponse.request().postDataJSON()).toMatchObject({ retailer: 'walmart', store_id: 'B', store_name: 'Store B' });
  const reviewAId = (await storeBReview.stagedResponse.json()).review.id;
  expect(reviewAId).toBeTruthy();
  await expect(page.locator('#storeChangeReviewDialog')).toHaveAttribute('open', '');
  await expect(page.locator('#storeChangeCancelBtn')).toHaveClass(/is-primary/);
  await expect(page.locator('#storeChangeApproveBtn')).toHaveClass(/is-ghost/);
  await expect(page.locator('#storeChangeReviewBody')).toContainText('Current store: Store A');
  await expect(page.locator('#storeChangeReviewBody')).toContainText('Proposed store: Store B');
  await expect(page.locator('#storeChangeReviewBody')).toContainText('Store A detergent');
  await expect(page.locator('#storeChangeReviewBody')).toContainText('Store B detergent');
  await expect(page.locator('#storeChangeReviewBody')).toContainText('Store A dish soap');
  await expect(page.locator('#storeChangeReviewBody')).toContainText('needs attention');
  await page.screenshot({ path: '/tmp/rung-feature5-store-change-review-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/rung-feature5-store-change-review-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart.id).toBe(currentA.id);
  const cancelRequest = page.waitForResponse(response => new URL(response.url()).pathname.includes('/api/shopping/store-change/') && new URL(response.url()).pathname.endsWith('/cancel'));
  await page.locator('#storeChangeCancelBtn').click();
  const cancelled = await cancelRequest;
  expect(cancelled.status()).toBe(200);
  expect(new URL(cancelled.url()).pathname).toBe('/api/shopping/store-change/' + reviewAId + '/cancel');
  expect((await cancelled.json()).review.status).toBe('cancelled');
  await expect(page.locator('#storeChangeReviewDialog')).not.toHaveAttribute('open', '');
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.store_id).toBe('A');
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart.id).toBe(currentA.id);
  const afterCancelBudget = (await api(page, 'GET', '/api/budget/summary')).data;
  expect(afterCancelBudget.checking_balance).toBe(beforeReviewBudget.checking_balance);
  expect(afterCancelBudget.safe_to_spend.safe_to_spend_cents).toBe(beforeReviewBudget.safe_to_spend.safe_to_spend_cents);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-target="shopping"]').click();
  await expect(page.locator('#shoppingStoreName')).toHaveText('Store A');

  // A real foreign request against the cancelled A review is still denied;
  // it proves household scoping, unlike a made-up numeric id.
  await api(page, 'POST', '/api/auth/logout');
  await login(page, B);
  const foreignCancel = await api(page, 'POST', '/api/shopping/store-change/' + reviewAId + '/cancel', {});
  expect(foreignCancel.status).toBe(404);
  expect((await api(page, 'GET', '/api/shopping/current-cart')).data.cart).toBeNull();
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.store_id).toBeFalsy();
  expect((await api(page, 'GET', '/api/retail/product-block')).data.blocks).toEqual([]);

  await api(page, 'POST', '/api/auth/logout');
  await login(page, A);
  await page.locator('[data-target="shopping"]').click();
  const storeBApproveReview = await chooseStoreThroughUi(page, 'Store B', true);
  expect(storeBApproveReview.selectResponse.status()).toBe(409);
  expect(storeBApproveReview.stagedResponse.status()).toBe(200);
  const reviewBId = (await storeBApproveReview.stagedResponse.json()).review.id;
  expect(reviewBId).not.toBe(reviewAId);
  await expect(page.locator('#storeChangeReviewDialog')).toHaveAttribute('open', '');
  const approveRequest = page.waitForResponse(response => new URL(response.url()).pathname.includes('/api/shopping/store-change/') && new URL(response.url()).pathname.endsWith('/approve'));
  await page.locator('#storeChangeApproveBtn').click();
  const approved = await approveRequest;
  expect(approved.status()).toBe(200);
  expect(new URL(approved.url()).pathname).toBe('/api/shopping/store-change/' + reviewBId + '/approve');
  expect((await approved.json()).review.status).toBe('approved');
  const currentB = (await api(page, 'GET', '/api/shopping/current-cart')).data.cart;
  expect(currentB.lines[0].product_id).toBe('B-SKU');
  expect(currentB.id).not.toBe(currentA.id);
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.store_id).toBe('B');
  await page.locator('[data-target="copilot"]').click();
  await expect(page.locator('#copilotStoreTitle')).toHaveText('Store B');
  await expect(page.locator('#copilotStoreContext')).toContainText('Only you can change this store in Shopping');
  const afterApproveBudget = (await api(page, 'GET', '/api/budget/summary')).data;
  expect(afterApproveBudget.checking_balance).toBe(beforeReviewBudget.checking_balance);
  expect(afterApproveBudget.safe_to_spend.safe_to_spend_cents).toBe(beforeReviewBudget.safe_to_spend.safe_to_spend_cents);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('[data-target="shopping"]').click();
  await expect(selectedLine(page).locator('.cart-current-title')).toHaveText('Store B detergent');
  expect((await api(page, 'GET', '/api/settings/current-location')).data.selected_store.store_id).toBe('B');
  expect(selectedA.store_id).toBe('A');

  // Tie every Chromium resource error to the exact intentional request;
  // matching merely a sorted pile of generic 404/409 strings would allow an
  // unrelated failed resource to slip through.
  expect(intentionalDenials).toEqual([
    { method: 'POST', path: '/api/location/select-store', status: 409 },
    { method: 'POST', path: '/api/shopping/store-change/' + reviewAId + '/cancel', status: 404 },
    { method: 'POST', path: '/api/location/select-store', status: 409 }
  ]);
  expect(errors).toEqual(intentionalDenials.map(denial =>
    'Failed to load resource: the server responded with a status of ' + denial.status + (denial.status === 409 ? ' (CONFLICT)' : ' (NOT FOUND)')));
  expect(failed).toEqual([]);
  expect(requests.some(request => request.includes('/api/grocery/generate-pay-period-plan'))).toBe(true);
});
