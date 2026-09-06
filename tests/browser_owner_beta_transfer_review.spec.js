const { test, expect } = require('@playwright/test');
const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5053';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });
async function login(page) { await page.goto(ROOT, {waitUntil:'networkidle'}); await page.locator('#authEmail').fill('transfer-browser@example.com'); await page.locator('#authPassword').fill('browser-pass-123'); await page.locator('#authLoginBtn').click(); await expect(page.locator('#authDialog')).not.toBeVisible(); }
async function open(page) { await page.locator('[data-target="overview"]').click(); await page.locator('.overview-quiet-details summary').click(); }
test('served Possible Duplicates Match and Keep Separate finalize deterministically', async ({page}) => {
  await login(page); await open(page);
  const list=page.locator('#reconciliationList'); await expect(list).toContainText('Browser match'); await expect(list).toContainText('Browser separate');
  const before=await page.evaluate(()=>fetch('/api/budget/summary').then(r=>r.json()));
  const match=list.locator('.stage-line').filter({hasText:'Browser match'}); await match.getByRole('button',{name:'Match'}).click(); await expect(list).not.toContainText('Browser match');
  const separate=list.locator('.stage-line').filter({hasText:'Browser separate'}); await separate.getByRole('button',{name:'Keep Separate'}).click(); await expect(list).not.toContainText('Browser separate');
  const after=await page.evaluate(()=>fetch('/api/budget/summary').then(r=>r.json())); expect(after.account_state.checking_balance).toBe(before.account_state.checking_balance-180);
  await page.reload({waitUntil:'networkidle'}); await open(page); await expect(list).toContainText('No possible duplicates');
  await page.setViewportSize({width:390,height:844}); expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true);
});
