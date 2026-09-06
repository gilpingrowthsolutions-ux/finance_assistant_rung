const { test, expect } = require('@playwright/test');
const { execFileSync } = require('child_process');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5051';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });

async function api(page, path) {
  return page.evaluate((endpoint) => fetch(endpoint).then(response => response.json()), path);
}
async function openReview(page) {
  await page.locator('[data-target="overview"]').click();
  await page.locator('.overview-quiet-details summary').click();
  return page.locator('#reconciliationList');
}

test('served ambiguous income requires a final Match or Keep Separate decision and replay is inert', async ({ page }) => {
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('income-review-browser@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  const before = await api(page, '/api/budget/summary');
  const savingsBefore = await api(page, '/api/savings/state');
  expect(before.account_state.checking_balance).toBe(1350);
  expect(savingsBefore.pyf_transfer_options.protections).toHaveLength(2);

  const list = await openReview(page);
  await expect(list).toContainText('Possible duplicate income');
  await expect(list).toContainText('Acme payroll match');
  await expect(list).toContainText('Acme payroll separate');
  const match = list.locator('.stage-line').filter({ hasText: 'Acme payroll match' });
  await match.getByRole('button', { name: 'Match' }).click();
  await expect(list).not.toContainText('Acme payroll match');
  expect((await api(page, '/api/budget/summary')).account_state.checking_balance).toBe(1350);
  expect((await api(page, '/api/savings/state')).pyf_transfer_options.protections).toHaveLength(2);

  const separate = list.locator('.stage-line').filter({ hasText: 'Acme payroll separate' });
  await separate.getByRole('button', { name: 'Keep Separate' }).click();
  await expect(list).toContainText('No possible duplicates');
  expect((await api(page, '/api/budget/summary')).account_state.checking_balance).toBe(1500);
  expect((await api(page, '/api/savings/state')).pyf_transfer_options.protections).toHaveLength(3);

  await page.reload({ waitUntil: 'networkidle' });
  const afterReload = await openReview(page);
  await expect(afterReload).toContainText('No possible duplicates');
  const replay = execFileSync(process.env.RUNG_PYTHON || '.venv/bin/python',
    ['tests/replay_owner_beta_income_provider.py'], { cwd: process.cwd(),
      env: { ...process.env, PYTHONPATH: process.cwd() }, encoding: 'utf8' });
  expect(replay).toContain("'applied': 0");
  expect((await api(page, '/api/budget/summary')).account_state.checking_balance).toBe(1500);
  expect((await api(page, '/api/savings/state')).pyf_transfer_options.protections).toHaveLength(3);
});
