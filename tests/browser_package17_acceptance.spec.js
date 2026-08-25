const { test, expect } = require('@playwright/test');
const ROOT='http://127.0.0.1:5054';

async function login(page){await page.goto(ROOT+'/',{waitUntil:'networkidle'});await page.waitForFunction(()=>document.querySelector('#authDialog')?.open||typeof window.rungOpenTab==='function');if(await page.locator('#authDialog').isVisible()){await page.locator('#authEmail').fill('recap-browser@example.com');await page.locator('#authPassword').fill('browser-pass-123');await page.locator('#authLoginBtn').click();await page.waitForLoadState('networkidle');}}
async function financialSnapshot(page){return page.evaluate(async()=>{const paths=['/api/budget/summary','/api/transactions','/bills','/api/savings/state','/api/payday-recap'];const rows=await Promise.all(paths.map(path=>fetch(path).then(r=>r.json())));return {safe:rows[0].safe_to_spend,account:rows[0].account_state,transactions:rows[1],bills:rows[2],savings:rows[3],recap:rows[4]};});}

test('Package 17 served Payday Recap is truthful, read-only, and responsive',async({page})=>{
  test.setTimeout(60000);const errors=[],failed=[],mutations=[];
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('pageerror',e=>errors.push(e.message));page.on('requestfailed',r=>failed.push(r.url()));page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method()))mutations.push({method:r.method(),path:new URL(r.url()).pathname})});
  await login(page);mutations.length=0;const before=await financialSnapshot(page);
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('$0.00');
  await expect(page.locator('#overviewRecapValue')).toHaveText('Finished $20.00 ahead');
  const originalRecap=before.recap;
  await page.screenshot({path:'/tmp/rung-package17-overview-desktop.png',animations:'disabled',fullPage:true});
  await page.locator('#openPaydayRecap').click();await expect(page.locator('#paydayRecapPanel')).toBeVisible();
  await expect(page.locator('#recapCycleDates')).toContainText('through');await expect(page.locator('#recapFinishValue')).toHaveText('Finished $20.00 ahead');
  await page.evaluate(()=>window.rungOpenTab('settings'));await page.locator('#settingsExpectedPaycheck').fill('1450');await page.locator('#updateRatiosBtn').click();
  await expect(page.locator('#settingsExpectedPaycheckStatus')).toContainText('Current plan: $1,000.00 · Next payday: $1,450.00');
  await page.locator('#settingsExpectedPaycheck').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/rung-package17-income-plan-settings-desktop.png',animations:'disabled'});
  const changed=await financialSnapshot(page);expect(changed.safe).toEqual(before.safe);expect(changed.transactions).toEqual(before.transactions);expect(changed.bills).toEqual(before.bills);expect(changed.savings).toEqual(before.savings);expect(changed.recap.finish_status).toBe(originalRecap.finish_status);expect(changed.recap.finish_amount_cents).toBe(originalRecap.finish_amount_cents);
  await page.evaluate(()=>window.rungOpenTab('transactions'));await expect(page.locator('#recapFinishValue')).toHaveText('Finished $20.00 ahead');
  await page.screenshot({path:'/tmp/rung-package17-recap-desktop.png',animations:'disabled',fullPage:true});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);
  await page.reload({waitUntil:'networkidle'});await page.evaluate(()=>window.rungOpenTab('settings'));await expect(page.locator('#settingsExpectedPaycheckStatus')).toContainText('Current plan: $1,000.00 · Next payday: $1,450.00');await page.evaluate(()=>window.rungOpenTab('transactions'));await expect(page.locator('#recapFinishValue')).toHaveText('Finished $20.00 ahead');
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>window.rungOpenTab('transactions'));await expect(page.locator('#paydayRecapPanel')).toBeVisible();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);await page.locator('#paydayRecapPanel').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/rung-package17-recap-mobile.png',animations:'disabled'});
  const unavailable={authority:'payday_recap_v1',read_only:true,status:'not_ready',completed_cycle:null,finish_status:'unavailable',finish_amount_cents:null,finish_reasons:['No confirmed income establishes that a prior pay cycle has completed yet.'],protected_summary:null,biggest_changes:[],completed_cycle_detail:null,current_cycle:{available:true,start_date:'2026-08-15',end_date:'2026-08-29'},next_payday:'2026-08-29',current_safe_to_spend_cents:10000,safe_to_spend_authority:'canonical_pyf_v1',informational_only:true,financial_mutations:false};
  await page.route('**/api/payday-recap',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(unavailable)}));await page.reload({waitUntil:'networkidle'});await expect(page.locator('#overviewRecapValue')).toHaveText('Recap not ready');await expect(page.locator('#overviewRecapSummary')).toContainText('No confirmed income');await page.unroute('**/api/payday-recap');
  const missing={...unavailable,status:'missing_setup',finish_reasons:['Complete authoritative pay-cycle setup before Rung can identify a finished cycle.'],current_cycle:{available:false,missing:['authoritative_pay_schedule']},current_safe_to_spend_cents:null};
  await page.route('**/api/payday-recap',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(missing)}));await page.reload({waitUntil:'networkidle'});await expect(page.locator('#overviewRecapSummary')).toContainText('Complete authoritative pay-cycle setup');await page.unroute('**/api/payday-recap');
  expect(mutations).toEqual([{method:'POST',path:'/api/account/update'}]);expect(failed).toEqual([]);expect(errors).toEqual([]);
});
