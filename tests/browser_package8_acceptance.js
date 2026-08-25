const { chromium } = require('playwright');

const baseURL = process.env.RUNG_BROWSER_URL || 'http://127.0.0.1:5051';

function product(title, id, price) {
  return { title, us_item_id: id, product_id: `p-${id}`, package_size: '1 ct', price, availability: 'in_stock', verified_location: true };
}

function line(base, title, id, price, alternatives, sourceKind, quantity, confidence) {
  return {
    keyword: base, resolved: true, packages_to_buy: quantity, estimated_price: price * quantity,
    selected_product: product(title, id, price), alternatives,
    selection_confidence: confidence || 'auto_selected', suggested: confidence !== 'user_selected',
    product_label: title, package_size: '1 ct', store_name: 'Walmart Supercenter Test', store_id: '357', retailer: 'walmart',
    confirmed_local_store: true,
    requirement: { item_name: base, base_item: base, source_kind: sourceKind, source_recipe_title: sourceKind === 'recipe' ? 'Test Supper' : null, source_text: `${quantity} ${base}`, quantity, unit: 'ct' }
  };
}

const originalCart = [
  line('flex cereal', 'Premium Flexible Cereal', 'flex-current', 12, [product('Value Flexible Cereal', 'flex-cheap', 5)], 'direct', 1),
  line('protected milk', 'Chosen Exact Milk', 'milk-explicit', 10, [product('Cheap Milk', 'milk-cheap', 2)], 'direct', 1, 'user_selected'),
  line('recipe chicken', 'Recipe Chicken Pack', 'chicken-current', 4, [], 'recipe', 2),
  line('direct soap', 'Direct Dish Soap', 'soap-current', 6, [], 'direct', 1),
  {
    ...line('unknown rice', 'Unknown Rice Package', 'rice-current', 9, [product('Small Rice', 'rice-cheap', 2)], 'recipe', 1),
    packages_to_buy: null, estimated_price: null, quantity_uncertain: true, package_resolution_uncertain: true,
    requirement: { item_name: 'unknown rice', base_item: 'unknown rice', source_kind: 'recipe', source_recipe_title: 'Test Supper', source_text: 'rice as needed', quantity: null, unit: null }
  }
];

function planPayload() {
  return {
    cart_items: JSON.parse(JSON.stringify(originalCart)), pantry_items_skipped: 0,
    grocery_tax_rate: 0, retailer: 'walmart', store_id: '357', store_name: 'Walmart Supercenter Test',
    store: { retailer: 'walmart', store_id: '357', name: 'Walmart Supercenter Test' },
    resolution_stats: { total_terms: 5 }, budget: { food_budget: 30, budget_remaining: -6, budget_exceeded: true }
  };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const requests = { preview: [], apply: [], plan: [] };
  page.on('response', response => {
    const url = response.url();
    if (url.includes('/api/grocery/rebalance/preview')) requests.preview.push(response.status());
    if (url.includes('/api/grocery/rebalance/apply')) requests.apply.push(response.status());
    if (url.includes('/api/grocery/generate-pay-period-plan')) requests.plan.push(response.status());
  });
  await page.route('**/api/meal-plan', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ recipe_ids: [] }) }));
  await page.route('**/api/grocery/generate-pay-period-plan', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(planPayload()) }));

  await page.goto(baseURL, { waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  await page.click('[data-tab="shopping"]');
  await page.evaluate(() => buildCart());
  await page.waitForSelector('#storeCartContainer .store-cart-item');
  const before = await page.locator('#storeCartContainer').innerText();
  if (!before.includes('Premium Flexible Cereal') || !before.includes('Chosen Exact Milk') || !before.includes('From Test Supper') || !before.includes('Direct Dish Soap')) throw new Error('controlled cart did not render all required authority states');

  await page.click('#rebalanceCartBtn');
  await page.waitForSelector('#rebalanceReviewDialog[open]');
  const previewText = await page.locator('#rebalanceReviewDialog').innerText();
  if (!previewText.includes('Premium Flexible Cereal') || !previewText.includes('Value Flexible Cereal') || !previewText.includes('Save $7.00')) throw new Error('before/after/savings preview missing: ' + previewText);
  if ((await page.locator('#storeCartContainer').innerText()) !== before) throw new Error('preview mutated cart');
  if (requests.preview.length !== 1 || requests.apply.length !== 0) throw new Error('preview network counts incorrect');

  await page.click('#rebalanceCancelBtn');
  if ((await page.locator('#storeCartContainer').innerText()) !== before || requests.apply.length !== 0) throw new Error('cancel mutated cart or applied');
  await page.reload({ waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  await page.click('[data-tab="shopping"]');
  await page.evaluate(() => buildCart());
  await page.waitForSelector('#storeCartContainer .store-cart-item');
  if (!(await page.locator('#storeCartContainer').innerText()).includes('Premium Flexible Cereal')) throw new Error('cancel did not survive reload unchanged');

  await page.click('#rebalanceCartBtn');
  await page.waitForSelector('#rebalanceReviewDialog[open]');
  await page.evaluate(() => { const button = document.querySelector('#rebalanceApplyBtn'); button.click(); button.click(); });
  await page.waitForFunction(() => document.querySelector('#storeCartContainer').innerText.includes('Value Flexible Cereal'));
  const applied = await page.locator('#storeCartContainer').innerText();
  if (!applied.includes('Chosen Exact Milk') || applied.includes('Cheap Milk') || !applied.includes('2 packages')) throw new Error('protected choice or required quantity changed');
  if (requests.apply.length !== 1 || requests.apply[0] !== 200) throw new Error('apply request count/status incorrect');

  await page.reload({ waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  await page.click('[data-tab="shopping"]');
  await page.evaluate(() => buildCart());
  await page.waitForFunction(() => document.querySelector('#storeCartContainer').innerText.includes('Value Flexible Cereal'));
  const reloaded = await page.locator('#storeCartContainer').innerText();
  if (!reloaded.includes('Chosen Exact Milk') || !reloaded.includes('Unknown Rice Package')) throw new Error('applied cart did not survive reload safely');

  await page.fill('#budgetInput', '20');
  await page.evaluate(() => { activeCartBudgetLimit = 20; });
  await page.click('#rebalanceCartBtn');
  await page.waitForSelector('#rebalanceReviewDialog[open]');
  const impossible = await page.locator('#rebalanceReviewDialog').innerText();
  if (!impossible.includes('could not safely meet this budget') || !impossible.includes('required quantities')) throw new Error('impossible budget was not reported truthfully');
  if ((await page.locator('#storeCartContainer').innerText()) !== reloaded) throw new Error('impossible preview mutated required/protected cart');

  console.log(JSON.stringify({
    preview_requests: requests.preview,
    apply_requests: requests.apply,
    plan_requests: requests.plan,
    preview_rendered: true,
    cancel_reload_unchanged: true,
    apply_reload_preserved: true,
    protected_choice_preserved: true,
    required_quantity_preserved: true,
    recipe_and_direct_present: true,
    unknown_quantity_preserved: true,
    impossible_budget_truthful: true,
    selected_store: 'walmart|357|Walmart Supercenter Test'
  }, null, 2));
  await browser.close();
})().catch(async error => {
  console.error(error.stack || error);
  process.exit(1);
});
