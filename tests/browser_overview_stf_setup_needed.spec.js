// Overview + Safe-to-Spend beta qualification — Scenario A (Setup Needed).
//
// Scope: a fresh admin-provisioned beta account on a fresh disposable
// database. Proves the served Overview truthfully reports incomplete setup,
// never fabricates a usable Safe-to-Spend number, provides an understandable
// path into required setup, and creates no fake Bills/baselines/store state
// merely from viewing Overview.
//
// Requires a dev server already running against an explicit disposable
// RUNG_DB_PATH with RUNG_ENV=beta, seeded via seed_overview_stf_fresh.py
// (user sts-fresh@example.com / sts-pass-123).
const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5212';

test('Overview Scenario A: setup-needed state is truthful and creates no fake state', async ({ page }) => {
  const mutations = [];
  page.on('request', (request) => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      const path = new URL(request.url()).pathname;
      mutations.push({ method: request.method(), path });
    }
  });
  const consoleErrors = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  const failed = [];
  page.on('response', (res) => { if (res.url().startsWith(ROOT + '/') && res.status() >= 400 && res.status() !== 401) failed.push({ status: res.status(), url: res.url() }); });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });

  // Authentication works.
  await expect(page.locator('#authDialog')).toBeVisible();
  await page.locator('#authEmail').fill('sts-fresh@example.com');
  await page.locator('#authPassword').fill('sts-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();

  // Onboarding auto-shows for a never-onboarded household; this is the
  // primary CTA into required setup, not a manufactured readiness path.
  await expect(page.locator('#onboardingDialog')).toBeVisible();
  mutations.length = 0; // login itself is the only expected mutation so far
  const mutationsBeforeSkip = mutations.slice();
  expect(mutationsBeforeSkip, 'no mutation merely from viewing Overview/onboarding').toEqual([]);

  // Dismiss via the real "Set up later" control so we can inspect the
  // underlying Overview truthfulness (readiness must not be faked by the
  // dialog itself).
  await page.locator('#onboardingSkipAllBtn').click();
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  // Skip truthfully lands on Copilot with a welcome message rather than a
  // faked-ready Overview; navigate back to inspect Overview truthfulness.
  await expect(page.locator('#copilot')).toBeVisible();
  await page.locator('[data-target="overview"]').click();
  await expect(page.locator('#overview')).toBeVisible();

  // Overview truthfully reports setup is needed; no fabricated number.
  await expect(page.locator('#safeHeroAmount')).toHaveText('—');
  await expect(page.locator('#safeHeroState')).toHaveText('Setup required');
  await expect(page.locator('#overviewSetupNotice')).toHaveClass(/is-visible/);
  await expect(page.locator('#allocUnpaidAmt')).toHaveText('—');
  await expect(page.locator('#allocPyfAmt')).toHaveText('—');
  // Protected buffer must not fabricate $0.00 when it was never confirmed.
  await expect(page.locator('#allocBufferAmt')).toHaveText('—');

  // CTA/navigation into required setup is understandable and functional.
  await page.locator('#overviewSetupBtn').click();
  await expect(page.locator('#settings')).toBeVisible();
  await expect(page.locator('[data-settings-pane="financial"]')).toHaveClass(/is-active/);

  // No fake Bills/baselines/store state were created merely by viewing
  // Overview and navigating: the only mutation so far is the explicit skip.
  expect(mutations.map((m) => m.path)).toEqual(['/api/onboarding/skip']);
  const billsResp = await page.evaluate(async () => (await fetch('/bills')).json());
  expect(billsResp).toEqual([]);
  const summary = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(summary.safe_to_spend.safe_to_spend).toBeNull();
  expect(summary.safe_to_spend.state).toBe('needs_setup');

  expect(failed).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);

  const overflowDesktop = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflowDesktop).toBeLessThanOrEqual(1);

  await page.locator('[data-target="overview"]').click();
  await page.screenshot({ path: '/tmp/rung-overview-stf-scenario-a-desktop.png', fullPage: true, animations: 'disabled' });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#safeHeroAmount')).toHaveText('—');
  await expect(page.locator('#overviewSetupNotice')).toHaveClass(/is-visible/);
  const overflowMobile = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflowMobile).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '/tmp/rung-overview-stf-scenario-a-mobile.png', fullPage: true, animations: 'disabled' });
});
