# Rung Pre-Beta State

Current verified repository state as of 2026-08-19. This file records current authority and open state, not package history.

## Current phase

- Friends-and-family beta hardening.
- Package 11 (first-run onboarding) is VERIFIED IMPLEMENTED and PASS.
- Package 12 is next and NOT STARTED. Package 11 did not implement location cleanup, nearby-store discovery changes, or selected-store consolidation.

## VERIFIED IMPLEMENTED

### Household and persistence safety

- Household-private financial, onboarding, preference, Plaid, reconciliation, and shopping state is household-scoped.
- Trusted household override headers require the configured HMAC signature; direct cross-household access is rejected.
- Household identity is database-unique by `Household.public_id` and non-null `Household.legacy_scope_key`.
- `Account.household_id` is database-unique (`uq_account_household_id`, migration `c6a4e2f9b731`), enforcing exactly one canonical Account per household across processes.
- Household and Account get-or-create paths recover from a database uniqueness race by rolling back the losing insert and loading the winning canonical row. Process-local locks remain an optimization only.
- Destructive database operations require an explicitly isolated disposable database. The protected historical SQLite path remains `/home/ky/finance_assistant/rung_finance.db`; production PostgreSQL is `rung_prod`.

### Financial authority

- `Account.checking_balance` is the checking-balance authority; writes use `services/financial_state.py` (`set_balance_absolute` / `apply_balance_delta`).
- A newly auto-created account has unknown checking balance, pay period, and expected paycheck. Legacy model defaults are not inserted as confirmed first-run facts.
- Pay Yourself First uses household `UserSetting` key `pyf_long_term_target_percent`.
- Protected checking buffer uses household `UserSetting` key `safe_to_spend_buffer_usd`.
- Manual pay schedule uses `Account.pay_period_days`, `Account.expected_paycheck`, and household `UserSetting` key `next_payday_date`; established income history remains a valid payday source.
- Grocery Need uses household `UserPreference` key `baseline_grocery_cost` and is the canonical grocery budget input.
- Fuel/transport Need uses the household gas-estimate `Bill` row; onboarding also retains `baseline_fuel_cost` as its repopulation preference.
- Recurring obligations use household `Bill` rows.
- Canonical readiness and Safe-to-Spend derive from explicit financial state, PYF target, actual/forecast Needs, protected buffer, and known payday/income state. Wizard completion is not financial authority.
- Missing checking balance, pay schedule/payday, current-period income, PYF target, buffer, grocery Need, or fuel Need remains explicit. `food_allocation_pct`, legacy `food_budget`, model defaults, and `is_onboarded` cannot complete canonical setup.

### Package 11 onboarding

- Manual-first onboarding persists checking balance, pay period, expected paycheck, next payday, PYF target, protected buffer, grocery Need, fuel Need, recurring bills, Shopping Style, Household Shopping Defaults, and Location Sharing to existing household authorities.
- Plaid is optional. Manual onboarding reaches complete canonical readiness without Plaid credentials or a Plaid item.
- `POST /api/onboarding/complete` validates late shopping/location fields before committing and commits the Package 11 mutation as one request transaction. A 400 response does not retain earlier financial writes.
- Revisit/save is idempotent for recurring bills and fuel Need. Omitted optional fields do not erase existing financial values, bills, style, or household defaults.
- The served onboarding controller registers one completion mutation path, repopulates saved values, and uses a viewport-bounded scrollable dialog consistent with the approved onboarding visual structure.

### Shopping preferences and store boundary

- Shopping Style and Household Shopping Defaults use household `HouseholdShoppingDefault` rows.
- Location Sharing uses household `UserSetting` key `location_sharing_enabled` (`true` / `false`). It permits device-location use/discovery only.
- Canonical selected shopping store remains controlled by `services/selected_store.py` and exact store identity state. Onboarding and Location Sharing do not create, select, or change it.
- Device location, nearby-store discovery, and selected shopping store remain distinct states. Store selection belongs in Shopping/Copilot, not onboarding or operational Settings.

### Other verified foundations

- Finished Shopping writes actual spend through the authoritative financial transaction path with idempotency support.
- Plaid connection/status, transaction sync, and manual/Plaid reconciliation foundations exist and remain optional.
- Shared exact Store x SKU retail foundation, provider waterfall/cost controls, and Rung-owned tax engine are implemented.
- PostgreSQL configuration/migrations and controlled SQLite-to-PostgreSQL migration tooling exist; disposable SQLite remains supported for development/tests.

## DECIDED BUT NOT IMPLEMENTED

- Package 12 location cleanup/consolidation is not started.
- Device location may discover nearby exact supported stores but must never automatically choose or replace the selected shopping store.
- Operational exact-store selection remains a Shopping/Copilot workflow.
- Plaid Link timing within onboarding remains deliberately undecided; Plaid must not become mandatory.
- The approved visual reference bundle remains the UI authority. Branding details marked flexible in the visual spec remain swappable.

## LEGACY / TO BE REPLACED

- `Account.food_allocation_pct` and compatibility `food_budget` output remain legacy-only and are not canonical grocery/PYF readiness inputs.
- Account model column defaults (including historical balance/paycheck/pay-period values) remain for legacy/demo compatibility but are explicitly cleared for automatic new-household creation.
- Some compatibility location fields on `Account` still expose bootstrap ZIP/store-name defaults; they are not a canonical exact selected store.
- Older store-name/keyword caches and provider-specific paths coexist with the exact Store x SKU foundation.
- Some frontend and backend compatibility surfaces remain larger than the approved final information architecture; Package 11 did not redesign them.

## KNOWN DEFECT

- Missouri local-tax ingestion is currently geographically limited: `MissouriDorQ3Adapter` imports detailed city/ZIP assignments only for Eldon/Versailles (`65026`/`65084`). Other Missouri selected-store locations can degrade to low-confidence state-only tax rather than full local jurisdiction tax. The statewide Missouri DOR source data exists, but ingestion must be generalized before Rung claims exact statewide local-tax coverage.

- The full Python suite passes but currently emits pre-existing deprecation warnings and a concurrency-test thread warning in Copilot idempotency coverage. Package 11 does not change that Copilot path.

## UNRESOLVED

- Final Plaid Link launch timing in onboarding.
- Package 12 implementation details for live device-location state, nearby discovery cleanup, and selected-store consolidation, subject to the locked location/store boundary above.
- Broader legacy compatibility cleanup outside Package 11.

## Verification baseline

- Package 11 data-integrity/authority/security focus: 86 passed, 2 skipped (PostgreSQL cases require an explicit disposable `POSTGRES_TEST_DATABASE_URL`).
- Separate-process SQLite creation acceptance: 8 workers converged on one Household and one truthful Account.
- Migration acceptance: duplicate preflight blocks without merging/deleting; valid existing Account values are preserved; SQLite unique enforcement passed. PostgreSQL cross-process acceptance is included and guarded for an explicitly named disposable test database.
- Full Python suite: 473 passed, 5 skipped.
- Frontend suite: 197 passed.
- Real Flask/Chromium Package 11 acceptance: 1 passed against `/tmp/rung_pkg11_integrity_browser.sqlite` at migration head `c6a4e2f9b731`; verified one household, one account, exact persisted input values, canonical readiness/grocery budget, reload/revisit preservation, no Plaid requirement, and no canonical selected-store mutation.
