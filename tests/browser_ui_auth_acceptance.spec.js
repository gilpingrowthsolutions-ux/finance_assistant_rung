// Authentication UI Convergence browser acceptance.
//
// Scope: Sign In, Logout, and session/unauthenticated handling converged to
// Board 04 (docs/visual/rung_visual_reference_bundle/04_RUNG_ONBOARDING_SETTINGS_AUTH.png).
// Create Account (self-service registration) has no backend authority in this
// repository and is intentionally NOT exercised here; the served UI's truthful
// "Self-service account creation is not currently supported" messaging is
// asserted instead so this test would fail if that truthful state regressed.
//
// Requires a dev server already running against an explicit disposable
// RUNG_DB_PATH with RUNG_ENV=beta, seeded via tests/seed_auth_acceptance.py
// (users auth-browser@example.com / auth-pass-123 and
// auth-browser-b@example.com / auth-pass-b-123).
const { test, expect } = require('@playwright/test');

const ROOT = process.env.RUNG_UI_BASE_URL || 'http://127.0.0.1:5099';

test('Auth: unauthenticated boundary blocks API and shows truthful Create Account state', async ({ page }) => {
  const apiRequests = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/')) apiRequests.push({ method: request.method(), path });
  });
  const consoleErrors = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(String(err)));

  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();
  expect(apiRequests.filter((r) => r.path === '/api/budget/summary')).toHaveLength(0);

  // Truthful Create Account state: no fake registration form, no OAuth buttons.
  await expect(page.locator('#authDialog')).toContainText('Self-service account creation is not currently supported');
  await expect(page.locator('#authDialog')).not.toContainText('Google');
  await expect(page.locator('#authDialog')).not.toContainText('Apple');
  expect(await page.locator('#authDialog form#authLoginForm').count()).toBe(1);
  expect(await page.locator('#authDialog input[type="email"]').count()).toBe(1);
  expect(await page.locator('#authDialog input[name="full_name"], #authDialog input#authFullName').count()).toBe(0);

  // Password field stays a real password input, with an accessible show/hide toggle
  // that does not itself submit or change credential authority.
  await expect(page.locator('#authPassword')).toHaveAttribute('type', 'password');
  await expect(page.locator('#authPassword')).toHaveAttribute('autocomplete', 'current-password');
  await expect(page.locator('#authEmail')).toHaveAttribute('autocomplete', 'username');
  await page.locator('#authPasswordToggle').click();
  await expect(page.locator('#authPassword')).toHaveAttribute('type', 'text');
  await page.locator('#authPasswordToggle').click();
  await expect(page.locator('#authPassword')).toHaveAttribute('type', 'password');

  expect(consoleErrors, 'no console errors on unauthenticated load').toEqual([]);
  expect(pageErrors, 'no page errors on unauthenticated load').toEqual([]);
});

test('Auth: invalid login shows truthful recoverable error with exactly one request', async ({ page }) => {
  const mutations = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') mutations.push(new URL(request.url()).pathname);
  });

  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();

  mutations.length = 0;
  await page.locator('#authEmail').fill('auth-browser@example.com');
  await page.locator('#authPassword').fill('totally-wrong-password');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authError')).toContainText('not recognized');
  await expect(page.locator('#authDialog')).toBeVisible();
  expect(mutations.filter((p) => p === '/api/auth/login')).toHaveLength(1);
  await expect(page.locator('#authLoginBtn')).toHaveText('Log In');
  await expect(page.locator('#authLoginBtn')).toBeEnabled();
});

test('Auth: rapid double Sign In submits once and resolves one session', async ({ page }) => {
  const logins = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/auth/login') logins.push(request);
  });
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('auth-browser@example.com');
  await page.locator('#authPassword').fill('auth-pass-123');
  await page.locator('#authLoginBtn').dblclick();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  expect(logins).toHaveLength(1);
  const current = await page.evaluate(async () => (await fetch('/api/auth/session')).json());
  expect(current.authenticated).toBe(true);
  expect(current.user.email).toBe('auth-browser@example.com');
});

test('Auth: sign in, logout, and sign back in resolve correct user/household with expected request counts', async ({ page }) => {
  const mutations = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') mutations.push(new URL(request.url()).pathname);
  });
  const consoleErrors = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();

  mutations.length = 0;
  await page.locator('#authEmail').fill('auth-browser@example.com');
  await page.locator('#authPassword').fill('auth-pass-123');
  await page.locator('#authLoginBtn').click();
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#authDialog')).not.toBeVisible();
  expect(mutations.filter((p) => p === '/api/auth/login')).toHaveLength(1);

  const session1 = await page.evaluate(async () => (await fetch('/api/auth/session')).json());
  expect(session1.authenticated).toBe(true);
  expect(session1.user.email).toBe('auth-browser@example.com');
  expect(session1.user.role).toBe('owner');
  const householdId = session1.household.id;

  await page.locator('[data-target="settings"]').click();
  await page.locator('[data-settings-section="account"]').click();
  await expect(page.locator('#settingsAccountEmail')).toHaveText('auth-browser@example.com');
  await expect(page.locator('#settingsAccountRole')).toHaveText('owner');

  // Reload preserves the authenticated session (no forced re-login).
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).not.toBeVisible();

  mutations.length = 0;
  await page.locator('[data-target="settings"]').click();
  await page.locator('[data-settings-section="account"]').click();
  await page.locator('#logoutBtn').click();
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#authDialog')).toBeVisible();
  expect(mutations.filter((p) => p === '/api/auth/logout')).toHaveLength(1);

  // Protected state is inaccessible after logout.
  const afterLogout = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(afterLogout.error).toBe('Authentication required.');

  mutations.length = 0;
  await page.locator('#authEmail').fill('auth-browser@example.com');
  await page.locator('#authPassword').fill('auth-pass-123');
  await page.locator('#authLoginBtn').click();
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#authDialog')).not.toBeVisible();
  expect(mutations.filter((p) => p === '/api/auth/login')).toHaveLength(1);

  const session2 = await page.evaluate(async () => (await fetch('/api/auth/session')).json());
  expect(session2.authenticated).toBe(true);
  expect(session2.household.id).toBe(householdId);
  expect(session2.user.email).toBe('auth-browser@example.com');

  // The test itself deliberately calls a protected endpoint after logout to
  // prove the 401 boundary holds; the browser logs that expected failed
  // fetch as a console error. Filter that single expected line out.
  const unexpectedConsoleErrors = consoleErrors.filter((msg) => !msg.includes('401'));
  expect(unexpectedConsoleErrors, 'no unexpected console errors across sign-in/logout/sign-in cycle').toEqual([]);
});

test('Auth: logout then another household login does not retain prior household authority or UI state', async ({ page }) => {
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await page.locator('#authEmail').fill('auth-browser@example.com');
  await page.locator('#authPassword').fill('auth-pass-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  const a = await page.evaluate(async () => (await fetch('/api/auth/session')).json());

  await page.locator('[data-target="settings"]').click();
  await page.locator('[data-settings-section="account"]').click();
  await page.locator('#logoutBtn').click();
  await expect(page.locator('#authDialog')).toBeVisible();

  await page.locator('#authEmail').fill('auth-browser-b@example.com');
  await page.locator('#authPassword').fill('auth-pass-b-123');
  await page.locator('#authLoginBtn').click();
  await expect(page.locator('#authDialog')).not.toBeVisible();
  const b = await page.evaluate(async () => (await fetch('/api/auth/session')).json());
  expect(b.user.email).toBe('auth-browser-b@example.com');
  expect(b.household.id).not.toBe(a.household.id);
  const budget = await page.evaluate(async () => (await fetch('/api/budget/summary')).json());
  expect(budget.safe_to_spend.checking_balance).toBe(222);
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).not.toBeVisible();
  await page.locator('[data-target="settings"]').click();
  await page.locator('[data-settings-section="account"]').click();
  await expect(page.locator('#settingsAccountEmail')).toHaveText('auth-browser-b@example.com');
});

test('Auth: responsive Sign In at desktop and mobile viewports', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(ROOT + '/', { waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();
  const desktopBox = await page.locator('.auth-shell').boundingBox();
  expect(desktopBox.width).toBeLessThan(500);
  const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(desktopOverflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '/tmp/rung-auth-signin-desktop.png', animations: 'disabled', timeout: 10000 });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('#authDialog')).toBeVisible();
  const mobileBox = await page.locator('.auth-shell').boundingBox();
  expect(mobileBox.x).toBeGreaterThanOrEqual(0);
  expect(mobileBox.x + mobileBox.width).toBeLessThanOrEqual(391);
  const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(mobileOverflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '/tmp/rung-auth-signin-mobile.png', animations: 'disabled', timeout: 10000 });

  // Keyboard accessibility: fields are reachable and labeled.
  await expect(page.locator('label[for="authEmail"]')).toBeVisible();
  await expect(page.locator('label[for="authPassword"]')).toBeVisible();
  await page.locator('#authEmail').focus();
  await expect(page.locator('#authEmail')).toBeFocused();
});
