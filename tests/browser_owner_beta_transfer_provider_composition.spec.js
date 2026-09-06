const { test, expect } = require('@playwright/test');
const { execFileSync } = require('child_process');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5051';
test.use({ launchOptions: { executablePath: process.env.RUNG_CHROMIUM_PATH } });

async function login(page) {
  await page.goto(ROOT, { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('physical-transfer-browser@example.com');
  await page.locator('#authPassword').fill('browser-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
}

function ingest(event) {
  const output = execFileSync(process.env.RUNG_PYTHON || '.venv/bin/python',
    ['tests/ingest_owner_beta_provider_event.py', event], {
      cwd: process.cwd(), env: { ...process.env, PYTHONPATH: process.cwd() }, encoding: 'utf8',
    });
  return JSON.parse(output.trim());
}

async function record(page, amount) {
  await page.locator('[data-target="savings"]').click();
  await expect.poll(() => page.evaluate(() => fetch('/api/savings/state').then(r => r.json())
    .then(x => x.pyf_transfer_options.protections.length))).toBe(1);
  await page.getByRole('button', { name: 'Record savings transfer' }).click();
  const dialog = page.locator('#physicalSavingsTransferDialog');
  await page.locator('#physicalSavingsAmount').fill(amount);
  const post = page.waitForRequest(r => new URL(r.url()).pathname === '/api/savings/transfer' && r.method() === 'POST');
  await dialog.getByRole('button', { name: 'Record transfer' }).click();
  expect((await post).postDataJSON().operation_id).toBeTruthy();
  await expect(dialog).not.toBeVisible();
}

test('served manual transfer then provider ingestion is reviewed once and Match replay is inert', async ({ page }) => {
  await login(page);
  await record(page, '80');
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  const staged = ingest('match');
  expect(staged).toMatchObject({ projection: { applied: 0, proposed: 1 }, checking: before.account_state.checking_balance, transfers: 1, proposals: 1, protections: 1 });

  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-target="overview"]').click();
  await page.locator('.overview-quiet-details summary').click();
  const list = page.locator('#reconciliationList');
  await expect(list).toContainText('Possible duplicate transfer');
  await expect(list).toContainText('TRANSFER TO SAVINGS');
  await list.getByRole('button', { name: 'Match' }).click();
  await expect(list).toContainText('No possible duplicates');
  const afterMatch = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  expect(afterMatch.account_state.checking_balance).toBe(before.account_state.checking_balance);
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-target="overview"]').click();
  await page.locator('.overview-quiet-details summary').click();
  await expect(list).toContainText('No possible duplicates');
  const replay = ingest('match');
  expect(replay).toMatchObject({ projection: { applied: 0, proposed: 0 }, checking: before.account_state.checking_balance, transfers: 1, proposals: 0, matched: 1, protections: 1 });
});

test('served Keep Separate produces exactly two transfer effects and provider replay stays final', async ({ page }) => {
  await login(page);
  await record(page, '30');
  const before = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  const staged = ingest('separate');
  expect(staged).toMatchObject({ projection: { applied: 0, proposed: 1 }, checking: before.account_state.checking_balance, transfers: 1, proposals: 1 });
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('[data-target="overview"]').click();
  await page.locator('.overview-quiet-details summary').click();
  const list = page.locator('#reconciliationList');
  await expect(list).toContainText('Possible duplicate transfer');
  await list.getByRole('button', { name: 'Keep Separate' }).click();
  await expect(list).toContainText('No possible duplicates');
  const afterDecision = await page.evaluate(() => fetch('/api/budget/summary').then(r => r.json()));
  expect(afterDecision.account_state.checking_balance).toBe(before.account_state.checking_balance - 30);
  const replay = ingest('separate');
  expect(replay).toMatchObject({ projection: { applied: 0, proposed: 0 }, checking: afterDecision.account_state.checking_balance, transfers: 2, proposals: 0, rejected: 1 });
});
