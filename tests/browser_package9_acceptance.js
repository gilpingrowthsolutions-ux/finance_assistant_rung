const { chromium } = require('playwright');

const baseURL = process.env.RUNG_BROWSER_URL || 'http://127.0.0.1:5052';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const network = { summary: [], expense: [], correction: [] };
  page.on('response', response => {
    const url = response.url();
    if (url.includes('/api/budget/summary')) network.summary.push(response.status());
    if (url.includes('/api/transactions') && response.request().method() === 'POST') network.expense.push(response.status());
    if (url.includes('/api/account/update') && response.request().method() === 'POST') network.correction.push(response.status());
  });

  await page.goto(baseURL, { waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  const feasible = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  const a = feasible.safe_to_spend;
  if (a.authority !== 'canonical_pyf_v1' || a.needs_total_cents !== 60000 || a.target_savings_cents !== 20000 || a.feasible_savings_cents !== 20000 || a.protected_buffer_cents !== 10000 || a.safe_to_spend_cents !== 30000) throw new Error('feasible arithmetic mismatch: ' + JSON.stringify(a));
  await page.click('[data-tab="overview"]');
  await page.waitForFunction(() => document.querySelector('#safeHeroAmount').textContent.includes('$300.00'));

  await page.evaluate(async () => fetch('/api/account/update', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({checking_balance: 800}) }));
  const infeasible = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  const b = infeasible.safe_to_spend;
  if (b.feasibility !== 'partial_target_feasible' || b.long_term_savings_target_percent !== 20 || b.feasible_savings_cents !== 10000 || b.savings_shortfall_cents !== 10000 || b.safe_to_spend_cents !== 0) throw new Error('infeasible arithmetic mismatch: ' + JSON.stringify(b));

  await page.evaluate(async () => fetch('/api/account/update', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({checking_balance: 1200}) }));
  await page.click('[data-tab="transactions"]');
  await page.fill('#tDesc', 'Controlled coffee');
  await page.fill('#tAmt', '25.00');
  await page.selectOption('#tCat', 'discretionary');
  await page.click('#logExpenseForm button[type="submit"]');
  await page.waitForFunction(() => document.querySelector('#transactionList').innerText.includes('Controlled coffee'));
  const afterExpense = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (afterExpense.safe_to_spend.safe_to_spend_cents !== 27500 || afterExpense.safe_to_spend.checking_cents !== 117500) throw new Error('expense recalculation mismatch');

  await page.reload({ waitUntil: 'networkidle' });
  await page.evaluate(() => { const d = document.querySelector('#onboardingDialog'); if (d && d.open) d.close(); });
  const reloaded = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (reloaded.safe_to_spend.safe_to_spend_cents !== 27500) throw new Error('expense did not persist across reload');

  await page.evaluate(async () => fetch('/api/transactions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({description:'Controlled income', amount:-100, category:'income'}) }));
  const afterIncome = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  if (afterIncome.safe_to_spend.safe_to_spend_cents !== 37500 || afterIncome.safe_to_spend.target_savings_cents !== 20000) throw new Error('income recalculation mismatch');

  if (network.expense.length !== 2 || network.expense.some(status => status !== 200)) throw new Error('financial transaction request count/status mismatch: ' + JSON.stringify(network));
  if (network.correction.length !== 2 || network.correction.some(status => status !== 200)) throw new Error('balance correction request count/status mismatch: ' + JSON.stringify(network));

  console.log(JSON.stringify({
    network,
    feasible: { checking_cents: a.checking_cents, needs_cents: a.needs_total_cents, target_cents: a.target_savings_cents, feasible_savings_cents: a.feasible_savings_cents, buffer_cents: a.protected_buffer_cents, safe_cents: a.safe_to_spend_cents },
    infeasible: { target_percent: b.long_term_savings_target_percent, target_cents: b.target_savings_cents, feasible_savings_cents: b.feasible_savings_cents, shortfall_cents: b.savings_shortfall_cents, safe_cents: b.safe_to_spend_cents },
    activity: { after_expense_safe_cents: afterExpense.safe_to_spend.safe_to_spend_cents, reload_safe_cents: reloaded.safe_to_spend.safe_to_spend_cents, after_income_safe_cents: afterIncome.safe_to_spend.safe_to_spend_cents, final_checking_cents: afterIncome.safe_to_spend.checking_cents }
  }, null, 2));
  await browser.close();
})().catch(error => { console.error(error.stack || error); process.exit(1); });
