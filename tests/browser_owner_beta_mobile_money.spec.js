const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL;
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function login(page) {
  await page.setViewportSize({width:390,height:844}); await page.goto(ROOT,{waitUntil:'networkidle'});
  await page.locator('#authEmail').fill('owner-beta-timeline@example.com'); await page.locator('#authPassword').fill('browser-pass-123'); await page.locator('#authLoginBtn').click(); await expect(page.locator('#authDialog')).not.toBeVisible();
}
test('mobile Money transaction dialog keeps an income entry usable', async ({page}) => {
  await login(page); await page.locator('[data-target="transactions"]').click(); await page.getByRole('button',{name:'Transactions',exact:true}).click();
  await expect(page.locator('#openTransactionDialog')).toBeVisible(); await page.locator('#openTransactionDialog').click(); const d=page.locator('#moneyTransactionDialog');
  await page.locator('#tDesc').fill('Mobile income'); await page.locator('#tAmt').fill('1'); await page.locator('#tCat').selectOption('income');
  await expect(d.getByRole('button',{name:'Add transaction'})).toBeVisible(); await expect(d.getByRole('button',{name:'Cancel'})).toBeVisible();
  expect(await d.evaluate(el => el.scrollHeight >= el.clientHeight)).toBe(true); expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);
  await d.getByRole('button',{name:'Cancel'}).click(); await expect(d).not.toBeVisible();
});
