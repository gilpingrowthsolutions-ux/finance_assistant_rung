# Rung Pre-Beta State

Current repository and environment state as re-grounded on 2026-08-24. Durable owner decisions live in `docs/RUNG_DECISION_LEDGER.md`; this file records implementation and qualification state.

## Repository baseline

- Repository: `/home/ky/finance_assistant`
- Branch: `main`
- HEAD: `88c7f955eb34f712f1f63d4538d342197e945056` (`Checkpoint Rung verified state through Package 11`)
- The working tree is materially dirty. Packages 13–17 and later hardening exist in modified and untracked files and are not represented by the HEAD commit.
- Preserve existing owner work. Qualification reports must identify the exact commit plus working-tree state they exercised.

## Runtime authority

Rung is one shared application source tree executed by two Python environments:

- `.venv` — REAL / ORIGINAL / CURRENT owner runtime.
- `venv` — BETA / TESTING runtime used for controlled testing and disposable acceptance.

Both currently use `/usr/bin/python3.14` / Python 3.14.4 and load the same repository `app.py`, templates, static files, services, models, and migrations. Neither virtual environment contains a copied Rung application. They are not interchangeable because their installed packages and intended data/configuration roles differ.

Important current dependency differences:

- `.venv` has Flask-SQLAlchemy 3.1.1; `venv` has the requirements-pinned 3.0.5.
- Both have Flask 3.0.0, SQLAlchemy 2.0.51, Flask-Migrate 4.1.0, Alembic 1.19.1, psycopg2-binary 2.9.12, Plaid 27.0.0, recipe-scrapers 15.11.0, and pytest 9.1.1.
- `venv` additionally contains PostgreSQL test helpers including `testing.postgresql`, `pg8000`, and `pglite`, plus unrelated analysis/application packages. `.venv` contains browser-automation-related Python packages not present in `venv`, but neither environment contains Python Playwright.
- Browser acceptance uses system Node/npm and an external Playwright package/browser cache, not a Playwright installation owned by either Python environment.
- Both contain Flask and Alembic command entrypoints; neither contains Gunicorn.

Historical scripts invoke `.venv` and `venv` inconsistently. Therefore every runtime, test, or browser claim must state the exact interpreter used and the resolved database target. Results from `venv` do not prove `.venv` behavior; if both are checked, report them separately.

## Configuration and database resolution

Both environments execute the same configuration code and load repository `.env` and optional `.env.local` before resolving the database. Resolution order is:

1. `RUNG_DB_PATH` — explicit SQLite override;
2. `DATABASE_URL`;
3. `/home/ky/finance_assistant/rung_finance.db` local fallback.

The current repository `.env` configures relative `sqlite:///finance.db`; absent an explicit process override, ordinary local startup resolves that to `/home/ky/finance_assistant/instance/finance.db` in either environment. The protected historical SQLite database remains `/home/ky/finance_assistant/rung_finance.db`.

Runtime role, not different application code, establishes which data may safely be used. Mutation-capable beta acceptance under `venv` still requires an explicitly verified disposable database authority before application import. Do not use `.venv` normal data for disposable acceptance.

## Current development phase

- Current priority is approved mockups -> actual served UI convergence.
- Global Shell + Overview (Slice 1), Money (Slice 2), Savings + Goals (Slice 3), Meals / Recipes (Slice 4), and Shopping (Slice 5) are completed bounded convergence slices; continue with one approved-mockup slice at a time.
- After UI convergence, test features one at a time and fix each before moving on.
- Do not substitute a giant all-at-once beta acceptance for this sequence.
- Physical geolocation is not automatically the next task.

## Verified by current code

Current code contains, without relying on old PASS reports:

- Household-scoped financial, savings, shopping, onboarding, Plaid, reconciliation, and Copilot authorities.
- `Account.checking_balance` writes through `services/financial_state.py`; balance correction does not fabricate an expense.
- Canonical Pay Yourself First feasibility and Safe-to-Spend over real/forecast Needs, protected buffer, and established income state.
- Append-only, effective-dated, integer-cent `IncomePlanVersion` expected-income history. Legacy `Account.expected_paycheck` is compatibility/prefill state rather than historical-cycle authority.
- Packages 13–14 Goals, named Reserves, Flexible Savings, Wealth destinations, append-only savings transfers, allocation runs, and confirmed allocation flow.
- Package 15 read-only Paycheck Timeline and informational Ahead/Behind with no Safe-to-Spend mutation.
- Package 16 deterministic Recurring Spending Watch and Savings Opportunities without a required background worker.
- Package 17 read-only Payday Recap resolving the income plan effective for the completed cycle.
- Copilot deterministic staging, review/confirmation, household operation identity, fingerprint checks, atomic finalization, replay handling, and bounded SQLite lock retry.
- Canonical selected-store service shared by Shopping and Copilot; discovery does not itself select a store.
- Active recipe persistence through household-scoped `MealPlanItem`; active recipe ingredients feed verified retailer-cart requirements.
- Rebalance preview/review/apply and Finished Shopping confirmation/financial-effect paths.
- Rung-owned tax decision boundary for carts, Rebalance, and Can-I-Buy. Unknown tax is not presented as zero, and legacy Account tax rates are not purchase authority.
- Manual-first onboarding, optional Plaid, authentication/session handling, PostgreSQL migrations/tooling, and controlled SQLite support.

These statements establish implementation presence, not current runtime/browser qualification.

## Actual served frontend

- `GET /` serves `templates/index.html` as a single-page interface.
- The browser executes `static/js/prepare-storage.js`, `recipes.js`, `grocery.js`, and `transactions.js`, plus substantial inline controllers in `templates/index.html`.
- The static modules and inline controllers form one hybrid served runtime; isolated JavaScript unit tests do not by themselves prove the integrated browser path.
- The approved visual specification and boards 01–05 remain authority; current UI is implementation evidence only. Global Shell + Overview, Money, Savings, Goals, Meals / Recipes, Shopping, and Copilot now materially follow the approved light hierarchy, while other screens are not claimed visually converged.
- Settings now uses focused controls/defaults sections with read-only location/store context; operational ZIP, nearby-store, and store-selection paths were retired from the consumer surface.

## Qualification status

- Global Shell + Overview convergence was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and explicit disposable SQLite target `/tmp/rung_ui_slice1_acceptance_20260824.sqlite`, set before application import.
- Real Chromium acceptance passed at 1440x1000 and 390x844: desktop sidebar, mobile Home/Copilot/Money/Shop/More navigation, canonical Overview values, setup/read/error behavior, cross-screen navigation, reload stability, no horizontal overflow, no unexpected local failed responses or console errors, and exactly one intended `/api/account/update` mutation when Update Balance was exercised.
- Focused `.venv` regressions passed for canonical Safe-to-Spend, Package 15 timeline, Package 17 recap, authentication boundary, served static JavaScript controllers, inline JavaScript syntax, and structural HTML. This is bounded slice qualification, not broad beta acceptance.
- Money UI Convergence Slice 2 was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and fresh disposable SQLite target `/tmp/rung_ui_slice2_money_20260824.sqlite`, set before application import.
- Real Chromium Money acceptance passed at 1440x1000 and 390x844: the Slice 1 shell remained intact; Accounts, Transactions, Bills, and Cash Flow views used canonical backend state; Timeline/Ahead-Behind, Payday Recap, and recurring observations remained informational/advisory; desktop/mobile navigation, reload stability, and overflow checks passed without unexpected local failed responses or console errors.
- The touched mutation smoke check produced exactly one transaction create, one Bill create, and one absolute checking-balance update per user action. Focused Money, canonical financial, Timeline, behavior-intelligence, Recap, reconciliation/sync, auth, JavaScript, and structural checks passed. This does not qualify other features or constitute broad beta acceptance.
- UI Convergence Slice 3 — Savings + Goals was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and fresh disposable SQLite target `/tmp/rung_ui_slice3_verified_20260824_6AkSNx.sqlite`, set before application import.
- Real Chromium acceptance passed at 1440x1000 and 390x844: canonical PYF and savings-ledger state populated distinct Reserves, Goals, Flexible Savings, and Wealth surfaces; allocation preview/cancel produced no mutation; one confirmed apply produced one allocation request; Goal create/edit/pause each produced one intended request; reload, navigation, responsive overflow, console, and local-request checks passed. Focused savings, PYF, behavior-intelligence, Copilot idempotency, household-isolation, auth, served JavaScript, inline syntax, and structural checks passed. This is bounded visual/workflow qualification, not broad feature or beta acceptance.
- UI Convergence Slice 4 — Meals / Recipes was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and fresh disposable SQLite target `/tmp/rung_slice4_acceptance_H4D4im/acceptance.sqlite`, set before application import.
- Real Chromium acceptance passed at 1440x1000 and 390x844: Browse All rendered eight real local catalog recipes; search preserved the local/external distinction; canonical recipe details and stored ingredient fields rendered; one Browse action created exactly one household `MealPlanItem`; reload preserved it; one Active Recipes removal returned the plan to zero; navigation and overflow checks passed without unexpected failed requests or console errors. Active Recipes is a display/management surface, not catalog selection. Focused recipe, active-plan, ingredient-fidelity, cart-adapter, Copilot recipe HITL, household-security, served JavaScript, inline-syntax, and structural checks passed. This is bounded visual/workflow qualification, not Shopping or broad recipe-feature acceptance.
- UI Convergence Slice 5 — Shopping was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and fresh disposable SQLite target `/tmp/rung_slice5_acceptance_2q299f/shopping.sqlite`, set before application import.
- Real Chromium acceptance passed at 1440x1000 and 390x844: no-store state blocked product resolution; one explicit canonical store selection survived reload and matched Copilot's shared selected-store read; one active-recipe requirement retained its saved `2 can` provenance without unsafe package conversion; one manual non-food requirement was created exactly once; same-store cached observations populated product/source states; unknown tax remained visibly excluded from the planned total; preview/cancel was zero-mutation; one approved Rebalance apply returned one cart choice; and one Finished Shopping confirmation created one completion, one `$30.00` expense, and one `$30.00` checking-balance effect. Reload, navigation, overflow, console, and local-request checks passed. Focused Shopping, resolver/preference/substitution/suggestion, optimizer/Rebalance, tax, Finished Shopping/reconciliation, household isolation, served JavaScript, inline syntax, structural HTML, and diff checks passed. Nearby discovery and physical device permission were not qualified by this slice.
- Slice 5's two interactive paths were requalified on 2026-08-24 with the REAL `.venv` runtime and fresh disposable SQLite target `/tmp/rung_slice5_ui_requal_verified_rHC8o3/requal.sqlite`. The actual visible Shopping controls selected the exact Walmart Versailles store with exactly one canonical selection request; reload, nearby refresh, and the shared Copilot store authority retained the same identity without a silent change. The visible Rebalance dialog issued one preview then zero apply requests on Cancel, issued a second preview on review, and its visible Apply button issued exactly one apply request and updated exactly one current-trip cart choice. Rebalance made no financial write; the focused Finished Shopping regression produced one completion, one expense, and one checking-balance effect.
- UI Convergence Slice 6 — Copilot was verified on 2026-08-24 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and fresh disposable SQLite target `/tmp/rung_copilot_slice6_requal_c7007z/copilot-final.sqlite`, set before application import. Actual visible Copilot controls at 1440x1000 and 390x844 answered a `$50.00` affordability prompt from the canonical `$735.00` Safe-to-Spend and truthfully reported `$685.00` remaining, with zero business/economic writes. The Safe-to-Spend explanation used current canonical checking, Needs, PYF, and buffer components and explicitly disclosed that complete verified before/after provenance is not available for attributing the exact historical change.
- Slice 6 staged-action regression used the visible review controls: staging and Cancel created zero Goals and zero apply requests; visible confirmation issued exactly one apply request and created exactly one Goal despite a double-click. Checking balance, existing transactions, Shopping requirements, MealPlanItems, product preferences, Shopping completions, and the shared selected store were unchanged. Desktop/mobile overflow, console, page, and local-request checks passed; focused canonical financial, Copilot staging/idempotency/stale-action, reconciliation, selected-store, household, auth, JavaScript, syntax, structural HTML, and diff checks passed. This is bounded UI/workflow qualification, not broad Copilot language coverage or beta acceptance.
- UI Convergence Slice 7 — Settings was verified on 2026-08-25 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and explicit disposable SQLite target `/tmp/rung_slice7_settings_final_verified_20260825.sqlite`, set and resolved before application import. Real Chromium acceptance passed at 1440x1000 and 390x844: desktop sidebar and mobile More -> Settings navigation remained intact; focused Settings sections rendered Financial Defaults, Shopping, Location, Notifications, and Account & Security without horizontal overflow or unexpected console/page/local-request errors.
- Slice 7 exercised exactly one visible PYF mutation (`/api/settings/pay-yourself-first`), one protected-buffer mutation (`/api/settings/safe-to-spend`), one household-shopping-default mutation (`/api/settings/household-shopping-defaults`), one Location Sharing mutation (`/api/settings/location-sharing`), and one expected-paycheck update (`/api/account/update`) for their respective edits. Reload preserved the Settings values. The paycheck fixture had a clearly established current plan: its visible edit retained `$2,100.00` for the current cycle and created a `$2,200.00` next-payday plan. Location Sharing did not change the canonical selected store; current location and selected store remained read-only, with store changes directed to Shopping. The expected-paycheck UI remains wired to effective-dated `IncomePlanVersion` authority through `/api/account/update`, not `Account.expected_paycheck` cycle mutation.
- The served consumer Settings UI retired collapsed Advanced ZIP maintenance, nearby-store discovery/exact-store selection, current-balance editing, legacy allocation inputs, vault work, and provider/internal usage controls. Backend services remain for their canonical Shopping/Copilot or internal owners. Notifications and bank-connection management are shown truthfully unavailable/manual-first because persisted consumer notification and connection-management controls are not present; account/security shows only authenticated session information when available. Focused Settings, income-plan, shopping-default, location-store, household/auth, inline-JavaScript, and diff checks passed. This is bounded Settings convergence, not physical geolocation, authentication, onboarding, or broad beta acceptance.
- VERIFIED IMPLEMENTED (2026-08-25): Canonical required-expense onboarding review/readiness foundation. The REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`) ran focused tests only with each test module setting the explicit disposable SQLite authority `RUNG_DB_PATH=:memory:` before importing the application; the command environment additionally used isolated `/tmp/rung_slice8a_foundation_tests.sqlite` and `/tmp/rung_slice8a_regressions.sqlite` targets. A household-scoped durable `UserSetting` distinguishes unanswered, explicit reviewed-none, has-expenses pending review, and has-expenses reviewed; explicit none creates no Bill, expense transaction, grocery baseline, or fuel baseline. Readiness recognizes reviewed-none as known-zero presence without changing Safe-to-Spend arithmetic; skip leaves the state unanswered. Focused onboarding/readiness, Safe-to-Spend, PYF, income-plan, household isolation, and auth-boundary regressions passed (102 tests).
- UI Convergence Slice 8 — Onboarding was completed and verified on 2026-08-25, continuing Codex's partial required-first Page 1 controller rather than restarting it. The REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) ran the full Python suite (587 passed, 6 skipped) and the served-JavaScript smoke suites (`node tests/test_grocery.js`, `node tests/test_transactions.js`; 43 and 57 passed) against explicit disposable SQLite targets under `/tmp/`, each verified non-production before `db.create_all()` and before application import. Browser acceptance ran the existing `tests/browser_ui_slice8_onboarding.spec.js` (extended, not replaced) with Node Playwright 1.62.1 driving the cached system Chromium at `/home/ky/.cache/ms-playwright/chromium-1234`, one fresh disposable SQLite database and one fresh dev-server process per scenario since the served app has no test-reset endpoint and a shared database would let one scenario's completed onboarding hide the next scenario's fresh-household dialog.
- All four Slice 8 browser scenarios passed at 1440x1000 and 390x844 with zero console errors and no horizontal overflow: (1) the required-first Page 1 controller shows all financial fields under one visible "(Required)" marker (>=7 on Page 1, zero on later pages), (2) the explicit NO-expenses path creates zero Bills/ExpenseTransactions/baselines and reaches `no_expenses_reviewed` with truthful `readiness.complete = true`, (3) the explicit YES-expenses path expands review on Page 1 (no hidden later required page), writes exactly one real Bill with no duplicate, and reaches `has_expenses_reviewed`, (4) Location Sharing during onboarding produced zero change to the shared canonical selected-store/retailer state (`/api/settings/grocery-retailer` before/after were identical and non-canonical), and (5) Set up later dismisses onboarding and truthfully reports `readiness.complete = false` ("Setup needed") rather than fabricating readiness. Each scenario issued exactly the expected `/api/onboarding/*` request count with no duplicates.
- Browser acceptance surfaced one genuine defect, which was fixed and covered by a new regression: switching an in-progress YES-expense review back to an explicit NO without first finishing it left stale grocery/fuel/bill input values in the DOM; `/api/onboarding/complete` would otherwise have written those stale values as real Bills/baselines despite the explicit "no required expenses" answer. The fix clears those inputs client-side on the YES->NO transition and, as defense in depth, `onboarding_complete()` now ignores `recurring_bills`/`baseline_grocery_cost`/`baseline_fuel_cost` whenever the durable `required_expense_review` state already resolves to `no_expenses_reviewed`, so an explicit "no" answer is always authoritative over client-submitted stale fields. A new Playwright scenario and a new `tests/test_package11_onboarding_integration.py` case cover this directly.
- Six Python regression files predated the Slice 8A required-expense-review gate and seeded `baseline_grocery_cost`/a fuel `Bill` directly without ever recording an explicit review answer, so canonical readiness correctly reported them incomplete once the gate existed; `tests/test_legacy_authority_retirement.py`, `tests/test_copilot_read_only_financial.py`, `tests/test_paycheck_timeline_package15.py`, `tests/test_behavior_intelligence_package16.py`, `tests/test_tax_coverage_hardening.py`, and `tests/test_sync_api.py` were updated to also seed the explicit reviewed state, restoring 18 previously-failing tests to green without changing product behavior.
- This closes Onboarding Slice 8. Authentication is the next approved-mockup slice; do not begin it until this paragraph's evidence has been independently spot-checked if further changes touch onboarding, Safe-to-Spend readiness, or the required-expense-review authority.
- UI Convergence — Sign In / Logout / Session (Authentication, partial) was verified on 2026-08-25 using the REAL `.venv` interpreter (`/home/ky/finance_assistant/.venv/bin/python`, Python 3.14.4) and an explicit disposable SQLite target under `/tmp/`, resolved before application import, with `RUNG_ENV=beta` set to exercise the enforced authentication boundary. The existing `/api/auth/login`, `/api/auth/logout`, `/api/auth/session` routes and `services/auth_session.py` session/throttle authority were traced and left unchanged; only `templates/index.html`'s `#authDialog` Sign In presentation and its bound JavaScript were visually converged toward Board 04 (Rung brand mark, "Welcome back" heading, rounded card/inputs, a single primary Log In action, an accessible password show/hide toggle, and the existing truthful "Beta accounts are provisioned by your Rung organizer. Self-service account creation is not currently supported" messaging, restyled rather than replaced). Real Chromium acceptance (`tests/browser_ui_auth_acceptance.spec.js`) passed at 1440x1000 and 390x844: the unauthenticated boundary blocked `/api/budget/summary` before login and showed no Google/Apple or full-name controls; invalid credentials produced the truthful "not recognized" error with exactly one `/api/auth/login` request; sign-in, logout, and sign-back-in each issued exactly one intended request and resolved the same household/user identity; a direct fetch after logout was truthfully rejected with 401; and no horizontal overflow or unexpected console errors occurred at either viewport. Focused `tests/test_gate7_auth_boundary.py` and `tests/test_package11_creation_authority.py`, the full Python suite (587 passed, 6 skipped), and the served-JavaScript `tests/test_grocery.js` (43 passed) / `tests/test_transactions.js` (57 passed) suites all passed unchanged, confirming backend session/household/security authority was not modified.
- Create Account (self-service registration) was traced and found to have no backend authority: only the `flask beta-user-create` / `beta-user-assign-household` CLI commands create `User`/`Household`/`HouseholdMembership` rows; no public registration route, duplicate-email API, or password-policy endpoint exists. Per explicit owner direction during this workstream (2026-08-25), Create Account was left as the existing truthful "self-service account creation is not currently supported" state rather than inventing a public registration architecture; self-service registration policy remains an open owner decision (see Unresolved). Google/Apple OAuth shown on Board 04 remain correctly omitted since no provider integration exists.

- Historical test, PostgreSQL, and browser reports remain historical evidence only.
- The 2026-08-24 recovery audits did not rerun Python suites, JavaScript suites, migrations, PostgreSQL gates, browser acceptance, beta acceptance, or physical-device checks.
- Packages 13–17 are present in code, but their historical PASS claims have not been freshly revalidated against the current dirty tree under either `.venv` or `venv`.
- No blanket current beta-ready or all-packages-PASS claim is established by the recovery audit.
- Any future claim must identify source state, `.venv` or `venv`, configuration, and disposable/non-disposable database target as appropriate.

## Legacy / current gaps

- `compute_liquidity_metrics()`, `Account.food_allocation_pct`, `food_budget`, `safe_disposable_cash`, and `free_cash_remaining` remain compatibility-only.
- Legacy Account defaults and location/store mirror fields coexist with newer authorities.
- Older store-name/keyword caches and provider-specific paths coexist with exact Store x SKU foundations.
- Legacy location/store backend compatibility remains, but its operational ZIP/store UI is retired from consumer Settings; Shopping and Copilot retain their canonical store workflows.
- The served frontend remains split between static modules and large inline controllers.
- Screens outside Global Shell + Overview, Money, Savings, Goals, Meals / Recipes, Shopping, and Copilot still require approved-mockup convergence of their hierarchy, component language, and shared states.

## Known defects

- The legacy transaction-delete route removes the transaction row without an explicit checking-balance reversal workflow. This was identified during Money Slice 2 and was not changed or requalified in Savings + Goals Slice 3.
- The canonical `seed_recipes.py` command currently leaves its Flask application context before its exception rollback path. Under the Slice 4 disposable `.venv` database this caused the bulk seed command to fail before catalog insertion. Slice 4 acceptance used the same canonical models and ingredient parser inside one verified disposable application context; the seed utility itself was not changed in this visual slice.
- `tests/browser_package18_20_acceptance.spec.js` (pre-existing, predates this workstream and was not modified by it) clicks `#logoutBtn` without first activating the Settings "Account & Security" sub-nav pane (`[data-settings-section="account"]`); since `.settings-pane` defaults to `display:none` and only the "Financial Defaults" pane starts `is-active`, that click targets a hidden button and would time out. This was discovered while building the 2026-08-25 Authentication acceptance spec, which activates the sub-nav pane before clicking. Package 18-20 is not part of the qualified set in this document; this defect was not fixed as out of scope for the Authentication workstream.

## Unresolved

- Live physical-browser/OS geolocation permission behavior. Emulated or Playwright geolocation is not physical acceptance.
- Nearby-store radius, distance calculation, and ranking. Current discovery does not establish nearest-first behavior.
- The cause of the prior Recipes “Browse All” failure in one disposable acceptance environment. Browse All succeeded against the Slice 4 disposable `.venv` catalog, so the old failure was not reproduced, but its historical root cause is not proven; the current bulk-seed utility defect is relevant environment evidence, not a definitive diagnosis.
- Recipe rows remain globally stored while current-pay-period `MealPlanItem` state is household-scoped; privacy/ownership policy for user-created and imported recipes remains unresolved.
- Final Plaid Link timing during onboarding.
- Self-service registration policy/API. Confirmed still absent as of 2026-08-25: no public registration route exists, only admin CLI user provisioning; Create Account was deliberately left as a truthful unavailable state rather than implemented (see Qualification status).
- Broader exact local and national tax coverage.
- Exact historical Safe-to-Spend change attribution remains unavailable without a complete verified before/after provenance read model; Copilot explains current canonical components and must not invent causality.
- Copilot does not yet have a separately qualified canonical read model for affordability of an uncreated dated Goal; its existing Goal creation path remains staged and confirmed.
- Broader legacy compatibility cleanup and eventual frontend-runtime consolidation.

## Safety baseline

- Household isolation, human approval, idempotency, concurrency protection, and one economic effect per operation remain mandatory.
- PostgreSQL is canonical for beta/production. SQLite is local development or explicitly disposable test/acceptance storage only.
- Never perform destructive database work without resolving and verifying an explicitly disposable target first.
- When disposable SQLite acceptance relies on `RUNG_DB_PATH`, set it before importing the application. `app.testing=True` is not isolation.
- Do not mutate, migrate, reset, import, seed, or drop the protected historical SQLite database or an unverified PostgreSQL database.
