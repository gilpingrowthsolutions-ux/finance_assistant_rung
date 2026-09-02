const { test, expect } = require('@playwright/test');
if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) test.use({launchOptions:{executablePath:process.env.RUNG_PLAYWRIGHT_CHROMIUM}});
const ROOT=process.env.RUNG_UI_BASE_URL||'http://127.0.0.1:5051';
test('RC onboarding completion refreshes canonical Overview without reload',async({page})=>{
 const errors=[],pages=[],failed=[]; page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('pageerror',e=>pages.push(String(e)));page.on('requestfailed',r=>failed.push(r.url()));
 await page.goto(ROOT,{waitUntil:'networkidle'});await page.locator('#authEmail').fill('rc-onboarding@example.com');await page.locator('#authPassword').fill('rc-onboarding-pass');await page.locator('#authLoginBtn').click();
 await expect(page.locator('#onboardingDialog')).toBeVisible(); await page.locator('#onboardingBalance').fill('1000');await page.locator('#onboardingPayPeriod').fill('14');await page.locator('#onboardingNextPayday').fill('2026-09-14');await page.locator('#onboardingExpectedPaycheck').fill('1200');await page.locator('#onboardingPyfTarget').fill('10');await page.locator('#onboardingSafeBuffer').fill('100');await page.locator('input[name="onboardingExpenses"][value="no"]').check();
 await page.locator('#onboardingNextBtn').click();await page.locator('#onboardingSkipStepBtn').click();await page.locator('#onboardingNextBtn').click();await page.locator('#onboardingNextBtn').click();
 await expect(page.locator('#onboardingDialog')).not.toBeVisible();await expect(page.locator('#overview')).toBeVisible();
 const budget=await page.evaluate(()=>fetch('/api/budget/summary').then(r=>r.json()));expect(budget.safe_to_spend.components.protected_buffer).toBe(100);await expect(page.locator('#safeHeroAmount')).toHaveText('$780.00');
 await page.locator('[data-target="copilot"]').click();await page.locator('[data-target="overview"]').click();await expect(page.locator('#safeHeroAmount')).toHaveText('$780.00');expect(errors).toEqual([]);expect(pages).toEqual([]);expect(failed).toEqual([]);
});
