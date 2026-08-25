const { test, expect } = require('@playwright/test');

if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) {
  test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
}

const baseURL = process.env.RUNG_BROWSER_URL || 'http://127.0.0.1:5051';

function product(title, id, price) {
  return { title, us_item_id: id, product_id: id, package_size: '1 ct', price, availability: 'in_stock', verified_location: true };
}

test('Workstream 3 connected desktop and mobile acceptance', async ({ page }) => {
  const calls = { mealPlan: 0, selectStore: 0, preview: 0, apply: 0, finish: 0 };
  page.on('request', request => {
    const url = request.url(); const method = request.method();
    if (method === 'POST' && url.includes('/api/meal-plan')) calls.mealPlan++;
    if (method === 'POST' && url.includes('/api/location/select-store')) calls.selectStore++;
    if (method === 'POST' && url.includes('/api/grocery/rebalance/preview')) calls.preview++;
    if (method === 'POST' && url.includes('/api/grocery/rebalance/apply')) calls.apply++;
    if (method === 'POST' && url.includes('/api/grocery/finished-shopping/complete')) calls.finish++;
  });
  await page.route('**/api/location/nearby-stores', async route => {
    const request = route.request();
    const selected = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      status: 'ok', location: { zip_code: '65026', city_state: 'Eldon, MO', source: 'zip' },
      stores: [{ retailer: 'walmart', store_id: 'eldon-99', name: 'Walmart Supercenter — Eldon', address: '1802 S Business 54, Eldon, MO 65026', postal_code: '65026', distance_miles: 1.2 }],
      selected_store: selected.selected_store
    }) });
  });
  await page.route('**/api/grocery/generate-pay-period-plan', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    retailer: 'walmart', store_id: 'eldon-99', store_name: 'Walmart Supercenter — Eldon',
    store: { retailer: 'walmart', store_id: 'eldon-99', name: 'Walmart Supercenter — Eldon' },
    cart_items: [{ keyword:'cereal', resolved:true, packages_to_buy:1, estimated_price:12, selected_product:product('Premium Cereal','cereal-premium',12), alternatives:[product('Value Cereal','cereal-value',5)], selection_confidence:'suggested', suggested:true, product_label:'Premium Cereal', package_size:'1 ct', store_name:'Walmart Supercenter — Eldon', store_id:'eldon-99', retailer:'walmart', confirmed_local_store:true, requirement:{item_name:'cereal',base_item:'cereal',source_kind:'direct',source_text:'1 cereal',quantity:1,unit:'ct'} }],
    grocery_tax_rate:0, budget:{grocery_need_budget:8,food_budget:8,budget_remaining:-4,budget_exceeded:true}, resolution_stats:{total_terms:1}
  }) }));

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(baseURL, { waitUntil: 'networkidle' });
  await page.evaluate(() => { const d=document.querySelector('#onboardingDialog'); if(d&&d.open)d.close(); });

  const recipe = await page.evaluate(async () => {
    const response = await fetch('/api/recipes', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:'WS3 Supper',servings:4,ingredients:['2 lb chicken breast','1 cup rice']}) });
    return response.json();
  });
  await page.click('[data-tab="recipes"]');
  await page.evaluate(() => refreshRecipes());
  const recipeCheckbox = page.locator('input[data-recipe]').last();
  await recipeCheckbox.check();
  await expect.poll(async () => (await page.evaluate(async () => (await fetch('/api/meal-plan')).json())).count).toBe(1);
  expect(calls.mealPlan).toBe(1);
  await page.reload({ waitUntil:'networkidle' });
  await page.evaluate(() => { const d=document.querySelector('#onboardingDialog'); if(d&&d.open)d.close(); });
  await page.click('[data-tab="shopping"]');
  await expect(page.locator('#activeRecipesGrid')).toContainText('WS3 Supper');

  const beforeStore = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
  await page.click('#shoppingChangeStoreBtn');
  await expect(page.locator('#shoppingStoreDialog')).toHaveAttribute('open', '');
  await page.fill('#shoppingStoreZip','65026');
  await page.click('#shoppingFindByZipBtn');
  await page.click('#shoppingNearbyStores button');
  expect(calls.selectStore).toBe(1);
  await expect(page.locator('#shoppingStoreName')).toContainText('Eldon');
  const selected = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
  expect(selected.selected_store.store_id).toBe('eldon-99');

  await page.evaluate(() => buildCart());
  await expect(page.locator('#storeCartContainer')).toContainText('Premium Cereal');
  await expect(page.locator('#storeCartContainer')).toContainText('More options');
  const cartBefore = await page.locator('#storeCartContainer').innerText();
  await page.click('#rebalanceCartBtn');
  await expect(page.locator('#rebalanceReviewDialog')).toHaveAttribute('open','');
  await page.click('#rebalanceCancelBtn');
  expect(await page.locator('#storeCartContainer').innerText()).toBe(cartBefore);
  await page.click('#rebalanceCartBtn');
  await page.click('#rebalanceApplyBtn');
  await expect(page.locator('#storeCartContainer')).toContainText('Value Cereal');
  expect(calls.preview).toBe(2); expect(calls.apply).toBe(1);
  await page.screenshot({ path:'/tmp/rung_ws3_desktop.png', fullPage:true });

  const beforeMoney = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  await page.click('#completeShoppingTripBtn');
  await expect(page.locator('#finishedShoppingConfirmDialog')).toHaveAttribute('open','');
  await page.click('#finishedShoppingConfirmBtn');
  await expect(page.locator('#finishedShoppingStatus')).toContainText('totals were updated');
  expect(calls.finish).toBe(1);
  const afterMoney = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(afterMoney.safe_to_spend.checking_cents).toBeLessThan(beforeMoney.safe_to_spend.checking_cents);

  await page.reload({ waitUntil:'networkidle' });
  await page.evaluate(() => { const d=document.querySelector('#onboardingDialog'); if(d&&d.open)d.close(); });
  await page.click('[data-tab="copilot"]');
  await expect(page.locator('#copilotStoreContext')).toContainText('Eldon');
  await page.evaluate(async () => fetch('/api/location/nearby-stores',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_detect:true,latitude:38.1,longitude:-92.5})}));
  const afterGps = await page.evaluate(async () => (await fetch('/api/settings/current-location')).json());
  expect(afterGps.selected_store.store_id).toBe('eldon-99');

  await page.setViewportSize({ width:390, height:844 });
  await expect(page.locator('.sidebar')).toBeVisible();
  await expect(page.locator('.brand')).toBeHidden();
  await expect(page.locator('#mobileMoreBtn')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
  await page.click('#mobileMoreBtn');
  await expect(page.locator('#mobileMoreMenu')).toHaveAttribute('open','');
  await page.click('.mobile-more-destination[data-destination="recipes"]');
  await expect(page.locator('#recipes')).toBeVisible();
  await page.screenshot({ path:'/tmp/rung_ws3_mobile.png', fullPage:true });
});
