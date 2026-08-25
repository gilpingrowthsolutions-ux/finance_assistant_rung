const { chromium } = require('playwright');

const baseURL = process.env.RUNG_BROWSER_URL || 'http://127.0.0.1:5053';
const missingMode = process.env.RUNG_P10_MISSING_SETUP === '1';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const network = { plan: [], account: [], finishStage: [], finishComplete: [], summary: [] };
  page.on('response', response => {
    const url = response.url();
    const method = response.request().method();
    if (url.includes('/api/grocery/generate-pay-period-plan') && method === 'POST') network.plan.push(response.status());
    if (url.includes('/api/account/update') && method === 'POST') network.account.push(response.status());
    if (url.includes('/api/grocery/finished-shopping/stage') && method === 'POST') network.finishStage.push(response.status());
    if (url.includes('/api/grocery/finished-shopping/complete') && method === 'POST') network.finishComplete.push(response.status());
    if (url.includes('/api/budget/summary')) network.summary.push(response.status());
  });
  await page.goto(baseURL, { waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });

  if (missingMode) {
    const result = await page.evaluate(async () => {
      const r = await fetch('/api/grocery/generate-pay-period-plan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({recipe_ids:[]})});
      return {status:r.status, body:await r.json()};
    });
    if (result.status !== 409 || result.body.code !== 'grocery_budget_setup_required' || result.body.budget.available !== false) throw new Error('missing setup was not truthful: ' + JSON.stringify(result));
    if ('food_budget' in result.body.budget || 'safe_disposable_cash' in result.body) throw new Error('legacy fallback leaked into missing setup');
    console.log(JSON.stringify({ missing_setup: result, network }, null, 2));
    await browser.close();
    return;
  }

  const first = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (first.safe_to_spend.authority !== 'canonical_pyf_v1' || first.safe_to_spend.safe_to_spend_cents !== 30000) throw new Error('canonical display arithmetic mismatch');
  await page.click('[data-tab="overview"]');
  await page.waitForFunction(() => document.querySelector('#safeHeroAmount').textContent.includes('$300.00'));
  await page.evaluate(async () => fetch('/api/account/update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({food_allocation_pct:1})}));
  await page.reload({waitUntil:'networkidle'});
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  const unchanged = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (unchanged.safe_to_spend.safe_to_spend_cents !== 30000) throw new Error('legacy allocation changed canonical safe-to-spend');

  await page.click('[data-tab="shopping"]');
  await page.evaluate(() => buildCart());
  await page.waitForSelector('#storeCartContainer .store-cart-item');
  const defaultBudget = await page.evaluate(() => ({value: activeCartBudgetLimit, text: document.querySelector('#cartBudget').textContent, cart: document.querySelector('#storeCartContainer').innerText}));
  if (defaultBudget.value !== 200 || !defaultBudget.text.includes('$200.00') || !defaultBudget.cart.includes('Great Value Whole Milk')) throw new Error('canonical grocery default did not reach API/UI: ' + JSON.stringify(defaultBudget));

  await page.fill('#budgetInput', '45.67');
  await page.evaluate(() => buildCart());
  await page.waitForFunction(() => activeCartBudgetLimit === 45.67);
  const override = await page.evaluate(() => ({value: activeCartBudgetLimit, text: document.querySelector('#cartBudget').textContent}));
  if (override.value !== 45.67 || !override.text.includes('$45.67')) throw new Error('explicit override failed');
  const afterOverride = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (afterOverride.safe_to_spend.safe_to_spend_cents !== 30000) throw new Error('cart override mutated canonical financial state');

  const finish = await page.evaluate(async () => {
    const stageResp = await fetch('/api/grocery/finished-shopping/stage', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({planned_total:50, actual_total:50, use_planned_total:false, retailer:'walmart', store_name:'Walmart — Versailles', store_id:'357', cart_signature:'p10-browser-trip'})});
    const stage = await stageResp.json();
    const payload = {planned_total:50, actual_total:50, use_planned_total:false, retailer:'walmart', store_name:'Walmart — Versailles', store_id:'357', cart_signature:'p10-browser-trip', operation_id:stage.operation_id, confirm:true};
    const completeResp = await fetch('/api/grocery/finished-shopping/complete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    return {stageStatus:stageResp.status, completeStatus:completeResp.status, complete:await completeResp.json()};
  });
  if (finish.stageStatus !== 200 || finish.completeStatus !== 200 || finish.complete.already_completed) throw new Error('finished shopping failed');
  const afterFinish = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (afterFinish.safe_to_spend.components.groceries_remaining !== 150 || afterFinish.safe_to_spend.safe_to_spend_cents !== 30000 || afterFinish.safe_to_spend.checking_cents !== 115000) throw new Error('finished shopping arithmetic/double count mismatch: ' + JSON.stringify(afterFinish.safe_to_spend));
  await page.fill('#budgetInput', '');
  await page.evaluate(() => buildCart());
  await page.waitForFunction(() => activeCartBudgetLimit === 150);
  await page.reload({waitUntil:'networkidle'});
  const persisted = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (persisted.safe_to_spend.components.groceries_remaining !== 150 || persisted.safe_to_spend.checking_cents !== 115000) throw new Error('finished shopping state did not persist');

  if (network.account.length !== 1 || network.finishStage.length !== 1 || network.finishComplete.length !== 1 || network.plan.length !== 3) throw new Error('mutation/network counts mismatch: ' + JSON.stringify(network));
  console.log(JSON.stringify({
    network,
    canonical_safe_cents: first.safe_to_spend.safe_to_spend_cents,
    legacy_change_safe_cents: unchanged.safe_to_spend.safe_to_spend_cents,
    default_budget_cents: defaultBudget.value * 100,
    explicit_override_cents: override.value * 100,
    post_finish_grocery_remaining_cents: afterFinish.safe_to_spend.components.groceries_remaining * 100,
    post_finish_safe_cents: afterFinish.safe_to_spend.safe_to_spend_cents,
    persisted_checking_cents: persisted.safe_to_spend.checking_cents,
  }, null, 2));
  await browser.close();
})().catch(error => { console.error(error.stack || error); process.exit(1); });
