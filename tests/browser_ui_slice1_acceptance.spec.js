const { test, expect } = require('@playwright/test');

async function login(page) {
  await page.goto('http://127.0.0.1:5051/', { waitUntil: 'networkidle' });
  if (await page.locator('#authDialog').isVisible()) {
    await page.locator('#authEmail').fill('browser@example.com');
    await page.locator('#authPassword').fill('browser-pass-123');
    await page.locator('#authLoginBtn').click();
    await expect(page.locator('#authDialog')).not.toBeVisible();
  }
  await expect(page.locator('#safeHeroAmount')).not.toHaveText('—');
}

test('approved shell and Overview use canonical state without navigation writes', async ({ page }) => {
  const apiMutations = [];
  const failed = [];
  const consoleErrors = [];
  page.on('request', req => {
    const url = new URL(req.url());
    if (url.pathname.startsWith('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(req.method())) apiMutations.push({ method:req.method(), path:url.pathname });
  });
  page.on('response', res => { if (res.url().startsWith('http://127.0.0.1:5051/') && res.status() >= 400) failed.push({ status:res.status(), url:res.url() }); });
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page);
  apiMutations.length = 0;
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(page.locator('.brand-wordmark')).toHaveText('Rung');
  await expect(page.locator('#primaryNav')).toContainText('Overview');
  await expect(page.locator('#primaryNav')).not.toContainText('Can I Buy');
  const canonical = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  const currentBalance = Number(canonical.account_state.checking_balance);
  const nextBalance = currentBalance + 50;
  await expect(page.locator('#safeHeroAmount')).toHaveText('$' + Number(canonical.safe_to_spend.safe_to_spend).toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 }));
  await expect(page.locator('#kpiBalance')).toHaveText('$' + currentBalance.toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 }));
  await expect(page.locator('#allocUnpaidAmt')).toHaveText('$615.00');
  await expect(page.locator('#allocPyfAmt')).toHaveText('$250.00');
  await expect(page.locator('#allocBufferAmt')).toHaveText('$150.00');
  await expect(page.locator('#overviewTrajectoryValue')).not.toContainText('Loading');
  await page.screenshot({ path:'/tmp/rung-ui-slice1-desktop.png', fullPage:true, animations:'disabled' });
  for (const target of ['copilot', 'transactions', 'savings', 'goals', 'recipes', 'shopping', 'settings', 'overview']) {
    await page.locator(`[data-target="${target}"]`).click();
    await expect(page.locator(`#${target}`)).toBeVisible();
  }
  expect(apiMutations).toEqual([]);

  await page.locator('#overviewUpdateBalanceBtn').click();
  await expect(page.locator('#overviewBalanceDialog')).toBeVisible();
  await page.locator('#overviewBalanceInput').fill(String(nextBalance));
  await page.locator('#overviewBalanceSave').click();
  const expectedBalance = '$' + nextBalance.toLocaleString('en-US', { minimumFractionDigits:2, maximumFractionDigits:2 });
  await expect(page.locator('#kpiBalance')).toHaveText(expectedBalance);
  expect(apiMutations.filter(row => row.path === '/api/account/update')).toHaveLength(1);
  expect(apiMutations).toHaveLength(1);
  await page.reload({ waitUntil:'networkidle' });
  await expect(page.locator('#kpiBalance')).toHaveText(expectedBalance);
  expect(failed).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test('mobile shell uses Home, Copilot, Money, Shop, and More without overflow', async ({ page }) => {
  await page.setViewportSize({ width:390, height:844 });
  await login(page);
  await expect(page.locator('.mobile-topbar')).toBeVisible();
  await expect(page.locator('.sidebar .brand')).toBeHidden();
  const visibleLabels = await page.locator('#primaryNav > :visible').allInnerTexts();
  expect(visibleLabels.map(value => value.trim().replace(/^[^A-Za-z]+/, ''))).toEqual(['Home', 'Copilot', 'Money', 'Shop', 'More']);
  await expect(page.locator('#safeHeroAmount')).toBeInViewport();
  await page.locator('#mobileMoreBtn').click();
  await expect(page.locator('#mobileMoreMenu')).toBeVisible();
  for (const label of ['Savings', 'Goals', 'Meals', 'Settings']) await expect(page.locator('.mobile-more-destination', { hasText:label })).toBeVisible();
  await page.locator('#mobileMoreClose').click();
  await page.locator('[data-target="shopping"]').click();
  await expect(page.locator('#shopping')).toBeVisible();
  await page.locator('[data-target="overview"]').click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path:'/tmp/rung-ui-slice1-mobile.png', fullPage:true, animations:'disabled' });
});
