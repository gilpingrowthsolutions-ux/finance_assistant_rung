const { test, expect } = require('@playwright/test');

async function login(page) {
  await page.goto('http://127.0.0.1:5052/', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelector('#authDialog')?.open || typeof window.rungOpenTab === 'function');
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('timeline-browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await page.waitForLoadState('networkidle');
  }
}

async function authoritySnapshot(page) {
  return page.evaluate(async () => {
    const [budget, timeline, savings, transactions, bills] = await Promise.all([
      fetch('/api/budget/summary').then(r => r.json()), fetch('/api/paycheck-timeline').then(r => r.json()),
      fetch('/api/savings/state').then(r => r.json()), fetch('/api/transactions').then(r => r.json()),
      fetch('/bills').then(r => r.json())
    ]);
    return {safe: budget.safe_to_spend.safe_to_spend_cents, timeline, savings, transactions, bills};
  });
}

test('Package 15 served timeline is read-only, responsive, and truthful', async ({ page }) => {
  const consoleErrors = [];
  const failedRequests = [];
  const mutationRequests = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('pageerror', err => consoleErrors.push(err.message));
  page.on('requestfailed', req => failedRequests.push(req.url()));
  page.on('request', req => { if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method())) mutationRequests.push({method:req.method(), path:new URL(req.url()).pathname}); });
  await login(page);
  mutationRequests.length = 0; // exclude the explicitly authorized login write

  const before = await authoritySnapshot(page);
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('$0.00');
  await expect(page.locator('#overviewTrajectoryValue')).toContainText('ahead');
  await expect(page.locator('#overviewTrajectoryReason')).toContainText('Settled Needs');
  await page.screenshot({path:'/tmp/rung-package15-overview-desktop.png', animations:'disabled', fullPage:true});

  await page.locator('#openPaycheckTimeline').click();
  await expect(page.locator('#transactions')).toBeVisible();
  await expect(page.locator('#timelineCycleDates')).toContainText('through');
  await expect(page.locator('#paycheckTimelineList')).toContainText('completed');
  await expect(page.locator('#paycheckTimelineList')).toContainText('upcoming confirmed');
  await expect(page.locator('#paycheckTimelineList')).toContainText('forecast');
  await page.locator('#timelineDetails summary').click();
  await expect(page.locator('#paycheckTimelineFull')).toContainText('Coffee');
  await page.screenshot({path:'/tmp/rung-package15-timeline-desktop.png', animations:'disabled', fullPage:true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await page.reload({waitUntil:'networkidle'});
  await expect(page.locator('#overviewTrajectoryValue')).toContainText('ahead');
  const afterReload = await authoritySnapshot(page);
  expect(afterReload).toEqual(before);

  await page.setViewportSize({width:390,height:844});
  await page.locator('[data-target="transactions"]').click();
  await expect(page.locator('#paycheckTimelinePanel')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({path:'/tmp/rung-package15-timeline-mobile.png', animations:'disabled', fullPage:true});
  const afterMobile = await authoritySnapshot(page);
  expect(afterMobile).toEqual(before);

  await page.route('**/api/paycheck-timeline', route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({
    authority:'paycheck_timeline_v1', read_only:true, status:'unavailable', setup_needed:true,
    cycle:{available:false, missing:['authoritative_pay_schedule']}, events:[], important_events:[],
    trajectory:{status:'unavailable', amount_cents:null, amount:null, reasons:['Complete pay-cycle setup to compare this cycle truthfully.']}
  })}));
  await page.reload({waitUntil:'networkidle'});
  await expect(page.locator('#overviewTrajectoryValue')).toHaveText('Cycle status unavailable');
  await expect(page.locator('#overviewTrajectoryReason')).toContainText('Complete pay-cycle setup');
  await page.unroute('**/api/paycheck-timeline');
  expect(mutationRequests).toEqual([]);
  expect(failedRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
