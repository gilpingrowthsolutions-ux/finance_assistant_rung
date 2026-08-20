# Rung Architecture Contract

## 1. Product Mission

- Rung is a financial assistant for working households between paychecks.
- Core question: "How much of this money is actually safe for me to spend?"
- Core flow:
  - income
  - protect bills
  - protect buffer
  - Safe to Spend
  - grocery/household budget
  - shopping plan/cart
  - actual spending
  - financial state recalculation
- Safe to Spend is authoritative financial state and must not become a second conflicting budgeting engine.

## 2. Financial Integrity

- Financial correctness has higher priority than convenience.
- No duplicate financial effects.
- Consequential writes must remain idempotent where operation IDs/tokens are used.
- Finished Shopping actual spending must write through the authoritative financial transaction pipeline.
- Balance reconciliation sets the balance directly and must not create a fake expense.
- Shopping corrections adjust only the correct delta.
- Manual/Plaid reconciliation must never create duplicate economic effects.
- Financial race conditions or household data leakage are launch blockers.
- Financial state must remain correct under concurrent writes.

## 3. Human Authority

- Rung may calculate, recommend, rank, optimize, stage, and reconcile candidates.
- Rung must not silently:
  - spend money
  - place orders
  - make consequential financial changes
  - violate explicit product requirements
  - make unauthorized substitutions
  - turn a one-time choice into a saved preference
  - allow advertising/sponsorship to override user interest
- Consequential customer-facing actions retain human approval.

## 4. Household Isolation

- Household financial data, preferences, and private state must remain household-scoped.
- Shared retailer/store/product information must never expose:
  - household identity
  - household finances
  - private preferences
  - shopping history attributable to another household
- Shared retail observations and private household preference data are separate concepts.

## 5. Product Preference Authority

- Use this authority order:
  1. explicit current request
  2. exact saved favorite/SKU
  3. saved usual
  4. preferred brand/variant/package
  5. approved substitutions
  6. deterministic ranking
- Explicit current requests always override older preferences.
- A one-time selection is not automatically persisted.
- A substitute never becomes the usual automatically.
- Retailer product IDs must never cross-match retailers.
- UPC may be used only when safe as cross-retailer identity evidence.

## 6. Retail Scope

- Rung shopping supports both food and general household retail products, including:
  - groceries
  - cleaning products
  - detergent
  - toiletries
  - paper goods
  - pet supplies
  - baby supplies
  - batteries
  - other normal household purchases
- Do not architect shopping as recipe/grocery-only.

## 7. Exact Physical Store Requirement

- Confirmed current product information must be tied to the user's selected physical store.
- Store-specific data must include or resolve to:
  - retailer
  - physical store ID
  - store context/location
  - product identity
  - price
  - availability/fulfillment when supported
  - retrieval/observation timestamp
  - source
  - confidence/freshness
- Never present generic online pricing, a different store's price, historical seed data, or estimates as Confirmed Local.
- Price/availability storage must be keyed to exact retailer + physical store + product identity.
- City/ZIP may help discover stores, but must not substitute for exact store identity once selected.

## 8. Shared Retail Data Architecture

- Rung should use shared retailer data across households to reduce provider cost.
- Stable product identity may be shared broadly.
- Volatile observations are store-specific.
- Conceptually:
  - RetailProduct
    - retailer
    - retailer product ID
    - UPC where available
    - brand
    - title
    - package/size
    - variant/category
  - StoreProductObservation
    - retailer
    - physical store ID
    - retailer product ID
    - price
    - price type
    - availability/fulfillment
    - observed_at
    - source
    - confidence
  - RetailSearchCache
    - retailer
    - physical store ID
    - normalized query
    - matched product identities
    - observed_at
- The same store/product observation can benefit multiple households.
- Household preferences remain private.

## 9. Cache / Freshness Rules

- Rung should minimize external calls through shared caching.
- Different data may have different freshness periods:
  - product identity: long-lived
  - price: short/medium-lived
  - availability: shorter-lived
  - search results: bounded cache
- Stale data must never be represented as live/current.
- Cached values should carry timestamp/source/confidence.
- If stale data is still useful during provider failure, Rung may show it truthfully as recent/last-known information.
- When multiple requests simultaneously need the same stale Store x Product refresh, use single-flight/deduplication behavior so one upstream request performs the refresh rather than creating a request stampede.

## 10. Free-First / Unit-Economic Architecture

- Paid APIs are the exception, not the default.
- The goal is not permanent $0 operating cost.
- The goal is:
  - revenue per active household >> cost to serve that household
- Architecture priorities:
  1. deterministic/local application logic
  2. Rung-owned database/cache/reference data
  3. free/official provider APIs
  4. shared observations/cache reuse
  5. inexpensive provider fallback only when necessary
- Recurring per-user API dependencies should be avoided when Rung can own/import/cache the information.
- Every external provider must support:
  - usage telemetry
  - configurable limits
  - kill switch/degraded mode where appropriate
  - no surprise unlimited spending

## 11. Retail Provider Strategy

- Kroger/Gerbes:
  - use official Kroger API where available
  - retain exact physical-store isolation
  - cache results
  - availability wording must reflect actual provider certainty
- Walmart:
  - SerpApi must evolve toward emergency/cold-start/stale-data fallback, not permanent primary backend
  - shared Store x Product observations and known SKU reuse should absorb normal repeat traffic
  - do not build production architecture around bypassing retailer CAPTCHAs, authentication controls, anti-bot protections, or prohibited scraping behavior
- Provider architecture must remain replaceable behind normalized retailer interfaces.

## 12. Tax Architecture

- Paid tax APIs must not be required for core Rung operation.
- Long-term tax calculation should be Rung-owned and based on free/public/government reference data where practical.
- Tax should be based on the physical purchase/store jurisdiction, not merely the user's GPS position.
- At minimum the design must support distinction between:
  - grocery food
  - general merchandise
  - exempt/unknown classes where needed
- Do not label fallback/state-level estimates as exact local checkout tax.
- Finished Shopping actual total remains financial truth.

## 13. LLM Architecture

- Deterministic parsing and calculations come first.
- Use a small/low-cost LLM only when language ambiguity genuinely requires it.
- LLMs must not perform authoritative financial math or deterministic optimization that application code can perform.
- Provider costs/tokens remain metered and capped.
- Customer-facing provider credentials/BYOK are not required.

## 14. Plaid

- Plaid is optional.
- Manual-first Rung must remain functional without Plaid.
- Missing Plaid configuration must not make the core app unusable.
- Bank sync can be enabled only when properly configured.
- Existing encrypted Plaid data must never be destroyed/replaced merely because a key/provider is unavailable.

## 15. Database Safety

- CRITICAL: production database/data must never be destroyed to make tests pass.
- Historical production SQLite path: /home/ky/finance_assistant/rung_finance.db
- Never run:
  - db.drop_all()
  - drop_all()
  - destructive reset helpers
  - equivalent destructive schema operations
  against the imported Rung app unless:
  1. RUNG_DB_PATH is explicitly set to a disposable isolated DB BEFORE importing app
  2. resolved DB path/URI is verified non-production
- app.testing=True alone is NOT database isolation.
- Browser/acceptance tests must use isolated disposable data.
- Production integrity must be verified before/after risky acceptance work.

## 16. Production Database Direction

- The pre-beta plan is to evaluate PostgreSQL as the likely production database because Rung is intended for:
  - multiple households
  - concurrent financial writes
  - shared retail-cache writes
  - multiple application workers/instances
  - national growth
- SQLite remains valid development/history state until a controlled migration is explicitly approved.
- Do not perform an uncontrolled production migration.

## 17. Failure / Graceful Degradation

- Provider failure must not unnecessarily break Rung's financial core.
- Examples:
  - retailer live refresh unavailable -> use truthful recent/last-known data where acceptable
  - SerpApi quota exhausted -> do not exceed hard spending controls
  - LLM unavailable -> deterministic features continue
  - Plaid unavailable -> manual financial features continue
- Do not fabricate certainty.

## 18. National Architecture

- Do not geographically restrict Rung to Missouri.
- Architecture must support users nationwide where supported retailers/data are available.
- A new physical store should be able to cold-start without a developer manually hardcoding it.
- Product identity may be reusable nationally.
- Price/availability remain physical-store specific.

## 19. Concurrency / Scale

- Before public launch, Rung must be tested for:
  - many households simultaneously
  - same-household simultaneous writes
  - duplicate requests/double clicks
  - shared-cache refresh contention
  - provider outages
  - quota exhaustion
  - cold stores
  - multiple states/time zones
  - restart/recovery
  - backup/restore
  - database contention
- Financial discrepancies and household data leakage are absolute blockers.
- Scaling should ideally require adding capacity, not redesigning fundamental architecture.

## 20. Development Process

- Major work uses bounded work packages.
- Default workflow:
  - architecture decision
  - comprehensive coding prompt
  - inspect
  - implement
  - self-review
  - focused tests
  - fix failures
  - full regression
  - acceptance
  - final report
- Do not stop after planning unless there is a genuine material blocker.
- Minor naming/helper/UI-copy ambiguity should be resolved using the smallest solution consistent with existing Rung patterns.
- Ask for clarification only when a choice materially affects:
  - financial integrity
  - data loss
  - security
  - architecture
  - recurring cost
  - user authority
- Major architecture work: GPT-5.3-Codex.
- Narrow bugs/acceptance closure: GPT-5.4 mini.
- GPT-5.6 Sol only for genuine difficult blockers that defeat cheaper models.

## 21. Change Discipline

- Do not introduce speculative major features during pre-beta hardening.
- Current goal:
  - make the existing product production-like, cost-efficient, nationally supportable, concurrent, resilient, and testable before friends-and-family beta.
