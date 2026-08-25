const { test, expect } = require('@playwright/test');

const ROOT = 'http://127.0.0.1:5053';
async function login(page) {
  await page.goto(ROOT+'/', {waitUntil:'networkidle'});
  await page.waitForFunction(() => document.querySelector('#authDialog')?.open || typeof window.rungOpenTab === 'function');
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('behavior-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click(); await page.waitForLoadState('networkidle');
  }
}
async function snapshot(page) {
  return page.evaluate(async()=>{const paths=['/api/budget/summary','/api/savings/state','/api/transactions','/bills','/api/behavior-intelligence'];const rows=await Promise.all(paths.map(p=>fetch(p).then(r=>r.json())));return {safe:rows[0].safe_to_spend.safe_to_spend_cents,savings:rows[1],transactions:rows[2],bills:rows[3],intelligence:rows[4]};});
}

test('Package 16 intelligence, HITL, persistence, and responsive safety', async ({page}) => {
  test.setTimeout(60000);
  const errors=[], failed=[], mutations=[];
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text())}); page.on('pageerror',e=>errors.push(e.message));
  page.on('requestfailed',r=>failed.push(r.url())); page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method()))mutations.push({method:r.method(),path:new URL(r.url()).pathname})});
  await login(page); mutations.length=0;
  const initial=await snapshot(page);
  await expect(page.locator('#overviewBehaviorCard')).toBeVisible();
  expect(await page.locator('#overviewBehaviorCard .behavior-card').count()).toBeLessThanOrEqual(1);
  await page.screenshot({path:'/tmp/rung-package16-overview-desktop.png',animations:'disabled',fullPage:true});

  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#recurringWatchCards')).toContainText('Possible recurring charge');
  const first=page.locator('#recurringWatchCards .behavior-card').filter({hasText:'planet fitness'});
  await first.locator('.behavior-review').click(); await expect(page.locator('#behaviorReviewDialog')).toBeVisible();
  await expect(page.locator('#behaviorReviewBody')).toContainText('reconciled with bank activity');
  await page.locator('#closeBehaviorReview').click();
  const ignoredName=(await first.locator('h3').textContent()).trim(); await first.locator('.behavior-ignore').click();
  await expect(page.locator('#recurringWatchCards')).not.toContainText(ignoredName);
  const afterIgnore=await snapshot(page);
  expect(afterIgnore.safe).toBe(initial.safe); expect(afterIgnore.bills).toEqual(initial.bills); expect(afterIgnore.transactions).toEqual(initial.transactions);
  expect(mutations).toEqual([{method:'POST',path:'/api/behavior-intelligence/decision'}]);
  await page.reload({waitUntil:'networkidle'}); await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#recurringWatchCards')).not.toContainText(ignoredName);

  const remaining=page.locator('#recurringWatchCards .behavior-card').filter({hasText:'stream box'});
  await remaining.locator('.behavior-stage-bill').click(); await expect(page.locator('#copilotStageDialog')).toBeVisible();
  await expect(page.locator('input[data-stage-section="bills_added"][data-stage-field="name"]')).toHaveValue('Stream Box'); await page.locator('#copilotDiscardStageBtn').click();
  expect((await snapshot(page)).bills).toEqual(initial.bills);
  await page.locator('[data-target="transactions"]').click(); await page.locator('#recurringWatchCards .behavior-card').filter({hasText:'stream box'}).locator('.behavior-stage-bill').click();
  await page.locator('#copilotApplyStageBtn').click(); await expect(page.locator('#copilotStageDialog')).not.toBeVisible();
  const afterBill=await snapshot(page); expect(afterBill.bills.length).toBe(initial.bills.length+1);

  await page.locator('[data-target="savings"]').click();
  await expect(page.locator('#waysToSaveCards')).toContainText('Reduce 25%'); await expect(page.locator('#waysToSaveCards')).toContainText('Reduce 75%');
  const beforePreview=await snapshot(page); await page.locator('#waysToSaveCards .behavior-preview').first().click();
  await expect(page.locator('.behavior-plan-result').first()).toContainText('Nothing was changed');
  const afterPreview=await snapshot(page); expect(afterPreview.safe).toBe(beforePreview.safe); expect(afterPreview.savings).toEqual(beforePreview.savings); expect(afterPreview.bills).toEqual(beforePreview.bills);
  await page.screenshot({path:'/tmp/rung-package16-savings-desktop.png',animations:'disabled',fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);

  await page.setViewportSize({width:390,height:844}); await page.evaluate(()=>window.rungOpenTab('savings'));
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({path:'/tmp/rung-package16-savings-mobile.png',animations:'disabled',fullPage:true});

  await page.route('**/api/behavior-intelligence',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({recurring_candidates:[],opportunities:[],overview_opportunity:null,suppressed_count:0,empty:true})}));
  await page.reload({waitUntil:'networkidle'}); await page.evaluate(()=>window.rungOpenTab('savings'));
  await expect(page.locator('#waysToSaveCards')).toContainText('No meaningful Savings Opportunities'); await page.unroute('**/api/behavior-intelligence');

  expect(mutations.filter(x=>x.path==='/api/copilot/apply')).toHaveLength(1);
  expect(mutations.filter(x=>x.path==='/api/behavior-intelligence/decision')).toHaveLength(1);
  expect(mutations.filter(x=>x.path==='/api/behavior-intelligence/stage-recurring-bill')).toHaveLength(2);
  expect(mutations.filter(x=>x.path==='/api/behavior-intelligence/savings-preview')).toHaveLength(1);
  expect(mutations.filter(x=>/pyf|goal|reserve|savings\/transfer|budget/.test(x.path))).toEqual([]);
  expect(failed).toEqual([]); expect(errors).toEqual([]);
});
