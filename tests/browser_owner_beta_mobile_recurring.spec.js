const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL;
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function login(page) { await page.setViewportSize({width:390,height:844}); await page.goto(ROOT,{waitUntil:'networkidle'}); await page.locator('#authEmail').fill('owner-beta-timeline@example.com'); await page.locator('#authPassword').fill('browser-pass-123'); await page.locator('#authLoginBtn').click(); await expect(page.locator('#authDialog')).not.toBeVisible(); }
test('mobile recurring Bill authority remains readable and manageable', async ({page}) => {
  await login(page); await page.locator('[data-target="transactions"]').click(); await page.getByRole('button',{name:'Bills',exact:true}).click();
  await page.locator('#openBillDialog').click(); await page.locator('#bName').fill('Mobile repeating bill'); await page.locator('#bAmt').fill('12'); await page.locator('#bDate').fill('2026-10-01'); await page.locator('#bRecurrence').selectOption('monthly'); await page.locator('#addBillForm button[type="submit"]').click();
  const row=page.locator('#billsList .list-item').filter({hasText:'Mobile repeating bill'}); await expect(row).toContainText('Repeats · monthly'); await row.getByRole('button',{name:'Manage repeat'}).click();
  const d=page.locator('#recurringBillDialog'); await expect(d).toBeVisible(); await expect(page.locator('#recurringBillAmount')).toBeEditable(); await expect(page.locator('#recurringBillDate')).toBeEditable(); await expect(page.locator('#recurringBillCadence')).toBeEditable(); await expect(page.locator('#recurringBillActive')).toBeVisible(); await expect(d.getByRole('button',{name:'Save changes'})).toBeVisible(); await expect(d.getByRole('button',{name:'Cancel'})).toBeVisible();
  expect(await d.evaluate(el=>el.scrollHeight>=el.clientHeight)).toBe(true); expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true); await d.getByRole('button',{name:'Cancel'}).click();
});
