const {test,expect}=require('@playwright/test');
const ROOT=process.env.RUNG_UI_BASE_URL||'http://127.0.0.1:5053';
test.use({launchOptions:{executablePath:process.env.RUNG_CHROMIUM_PATH}});
async function login(page){await page.goto(ROOT,{waitUntil:'networkidle'});await page.locator('#authEmail').fill('two-cycle-browser@example.com');await page.locator('#authPassword').fill('browser-pass-123');await page.locator('#authLoginBtn').click();await expect(page.locator('#authDialog')).not.toBeVisible();}
async function state(page){return page.evaluate(()=>Promise.all([fetch('/api/budget/summary').then(r=>r.json()),fetch('/api/savings/state').then(r=>r.json()),fetch('/api/transactions').then(r=>r.json())]));}
test('prior-cycle PYF stays protected, current income creates distinct PYF, and explicit old fulfillment leaves STS unchanged',async({page})=>{
  await login(page);
  const before=await state(page), old=before[1].pyf_transfer_options.protections;
  expect(Number.isInteger(before[0].safe_to_spend.safe_to_spend_cents)).toBe(true);
  expect(old).toHaveLength(1); expect(old[0]).toMatchObject({income_description:'Prior-cycle payroll',remaining_cents:18000});
  expect(before[0].safe_to_spend.components.active_income_pyf_protection_cents).toBe(18000);
  expect(before[0].safe_to_spend.components.current_cycle_income_pyf_protection_cents).toBe(0);
  await page.locator('[data-target="transactions"]').click(); await page.getByRole('button',{name:'Transactions'}).click(); await page.getByRole('button',{name:'+ Add transaction'}).click();
  await page.locator('#tDesc').fill('Current-cycle payroll'); await page.locator('#tAmt').fill('1800'); await page.locator('#tCat').selectOption('income');
  const incomes=[]; page.on('request',r=>{if(new URL(r.url()).pathname==='/api/transactions'&&r.method()==='POST')incomes.push(r.postDataJSON());});
  await page.locator('#moneyTransactionDialog').getByRole('button',{name:'Add transaction'}).click(); await expect(page.locator('#moneyTransactionDialog')).not.toBeVisible();
  expect(incomes).toHaveLength(1); expect(incomes[0].operation_id).toBeTruthy();
  await expect.poll(async()=>((await state(page))[1].pyf_transfer_options.protections.length)).toBe(2);
  const afterIncome=await state(page), protections=afterIncome[1].pyf_transfer_options.protections;
  expect(afterIncome[0].account_state.checking_balance).toBe(before[0].account_state.checking_balance+1800);
  expect(afterIncome[2].filter(r=>r.category==='income')).toHaveLength(2);
  expect(protections.map(p=>p.remaining_cents)).toEqual([18000,18000]);
  const oldProtection=protections.find(p=>p.income_description==='Prior-cycle payroll'); const current=protections.find(p=>p.income_description==='Current-cycle payroll');
  expect(oldProtection).toBeTruthy(); expect(current).toBeTruthy();
  expect(afterIncome[0].safe_to_spend.components.current_cycle_income_pyf_protection_cents).toBe(18000);
  await page.locator('[data-target="savings"]').click(); await page.getByRole('button',{name:'Record savings transfer'}).click();
  const dialog=page.locator('#physicalSavingsTransferDialog'); await expect(dialog).toContainText('Prior-cycle payroll'); await expect(dialog).toContainText('Current-cycle payroll');
  const transfers=[]; page.on('request',r=>{if(new URL(r.url()).pathname==='/api/savings/transfer'&&r.method()==='POST')transfers.push(r.postDataJSON());});
  await page.locator('#physicalSavingsProtection').selectOption(String(oldProtection.id)); await page.locator('#physicalSavingsAmount').fill('180');
  const physicalBefore=await state(page); expect(Number.isInteger(physicalBefore[0].safe_to_spend.safe_to_spend_cents)).toBe(true); expect(physicalBefore[0].account_state.checking_balance).toBe(3600); expect(physicalBefore[0].safe_to_spend.safe_to_spend_cents).toBe(324000); await dialog.getByRole('button',{name:'Record transfer'}).click(); await expect(dialog).not.toBeVisible();
  expect(transfers).toHaveLength(1); expect(transfers[0].income_pyf_protection_id).toBe(oldProtection.id);
  const final=await state(page); expect(final[0].account_state.checking_balance).toBe(physicalBefore[0].account_state.checking_balance-180);
  expect(final[0].safe_to_spend.safe_to_spend_cents).toBe(324000); expect(final[0].safe_to_spend.safe_to_spend_cents).toBe(physicalBefore[0].safe_to_spend.safe_to_spend_cents);
  expect(final[1].pyf_transfer_options.protections).toHaveLength(1); expect(final[1].pyf_transfer_options.protections[0]).toMatchObject({id:current.id,remaining_cents:18000});
  await page.reload({waitUntil:'networkidle'}); await page.locator('[data-target="savings"]').click();
  const reloaded=await state(page); expect(reloaded[1].pyf_transfer_options.protections).toHaveLength(1); expect(reloaded[1].pyf_transfer_options.protections[0].id).toBe(current.id);
});
