# RUNG — CANONICAL PROJECT HANDOFF

**Recovered:** 2026-08-18  
**Purpose:** Replace the unreliable prior handoff with a clean separation between owner-approved product decisions and current repository truth.  
**Product:** **Rung**

---

## 1. AUTHORITY RULES

This document uses four statuses:

- **LOCKED PRODUCT DECISION** — explicitly requested/corrected by the owner or clearly approved.
- **VERIFIED IMPLEMENTED** — found by the current Codex repository/runtime audit.
- **CURRENT LEGACY / DEFECT** — code exists but conflicts with the intended product, is duplicated, or is not currently trustworthy.
- **UNRESOLVED** — prior conversations did not establish enough evidence to lock the detail.

Do not treat the old hallucinating-chat handoff as authoritative. Do not infer product intent from current code. Do not claim implementation merely because a feature was previously called "complete." If newer repository evidence conflicts with this document's implementation section, use the newer repository evidence and update the handoff.

The approved Rung visual reference images/spec remain the visual authority; this handoff does not replace them.

---

# 2. RUNG'S LOCKED PRODUCT MODEL

## Core promise

**LOCKED PRODUCT DECISION**

> **Rung automatically protects your savings and necessities, then shows you what's truly safe to spend until payday.**

Rung should require **less input over time**. It is not meant to become a manual budgeting ledger that repeatedly asks the household for information it can safely remember, infer, or retrieve.

## End-to-end product loop

**LOCKED PRODUCT DECISION**

**current financial reality** → protect savings/Needs/buffer → calculate Safe-to-Spend → plan meals/household needs → derive shopping requirements → discover nearby exact stores → user selects exact physical store → resolve real sellable products → build budget-aware cart → shop → Finished Shopping → reconcile manual/Plaid activity → recalculate financial reality → continue to payday → next cycle.

---

# 3. FINANCIAL MODEL

## Pay Yourself First

**LOCKED PRODUCT DECISION**

The old fixed 50/30/20 model is superseded. Rung uses **Pay Yourself First (PYF)**.

- The user chooses the long-term savings/protection target percentage.
- It is adjustable and not fixed at 20%.
- There is no arbitrary maximum percentage imposed by Rung.
- Rung must not silently change the user's long-term target.

Each pay period, Rung tests that target against actual/forecast Needs, required commitments, the protected checking buffer, and money actually available. If the full target is not safe this cycle, Rung calculates the feasible contribution, explains that the target is too aggressive **for this pay period**, recommends the temporary lower contribution, and leaves the long-term target unchanged.

## Needs, buffer, Wants, Safe-to-Spend

**LOCKED PRODUCT DECISION**

Needs are actual/forecast necessary expenses, not a percentage slider. Examples include bills, groceries, necessary fuel, prescriptions, childcare, household necessities, and other real required expenses.

Rung has a user-defined **protected checking buffer**. It is a real financial constraint, not a display preference.

Wants are nonessential spending remaining after required protection. **Safe-to-Spend is the practical everyday number Rung exposes to the user.**

Conceptually:

**available money** → desired PYF target → real Needs → protected buffer → feasibility check → remaining Safe-to-Spend/Wants.

A real unexpected Need may consume current Wants/Safe-to-Spend. If a shortfall remains, **Rung asks before using protected savings/reserves**; it does not silently raid them.

## Savings, reserves, wealth, goals

**LOCKED DIRECTION / SOME DETAILS UNRESOLVED**

Rung should help protect against statistically inevitable "unexpected" costs such as car repairs, medical expenses, HVAC/appliance failures, and similar household costs. Named reserve buckets were discussed, but the exact final taxonomy and redirect rules after a reserve is fully funded are **UNRESOLVED**.

Rung may provide basic long-term wealth/investment education, but should not initially auto-invest, auto-trade, choose individual securities, or guarantee outcomes. Moving money between wealth cash and investments is not ordinary spending.

Approved features include **Goals, Paycheck Timeline, Where Your Money Is Going, Ahead/Behind Plan, Recurring Spending Watch, Payday Recap, and goal affordability through Copilot**. Exact formulas/thresholds/rollover mechanics for these are **UNRESOLVED** and must not be invented.

Important rule: being ahead should remain meaningful; Rung should not automatically turn every underspend into additional spending permission.

---

# 4. RECIPES / MEALS / GROCERY — INTENDED PRODUCT PIPELINE

## Recipes → Active Recipes This Pay Period

**LOCKED PRODUCT DECISION**

**Recipes tab** → user chooses/sends recipes into the current pay period → those recipes become the active pay-period set → **Active Recipes This Pay Period** displays that set → Grocery consumes it.

**Critical rule:** the user does **not** select recipes from Active Recipes This Pay Period. Selection originates in **Recipes**.

Exact button/copy for activation is **UNRESOLVED** unless the approved visual reference specifies it.

## Active recipes → grocery requirements

**LOCKED DIRECTION**

Active recipes → deterministic ingredient normalization → preserve required quantities → safely combine compatible units → consider pantry coverage where known → keep unsafe conversions unresolved → remaining requirements become purchases → direct non-recipe household/shopping items join the same plan.

Exact formulas for household-size serving scaling, duplicate ingredient aggregation, leftovers, snacks, and pantry subtraction are **UNRESOLVED** unless explicitly decided later.

## Product/package correctness

**LOCKED PRODUCT DECISION**

Recipe requirement → package sizing → retailer product matching is deterministic, not LLM guessing. Rung must preserve quantity, make only safe unit conversions, avoid invented volume↔mass conversions and package sizes, choose a sellable package that adequately covers the requirement, reject wrong product forms, and keep uncertain matches unresolved.

---

# 5. SHOPPING / RETAIL MODEL

## Exact selected physical store

**LOCKED PRODUCT DECISION**

Three concepts remain separate:

1. **Current device location** — where the user is now.
2. **Nearby-store discovery** — exact supported physical stores nearby.
3. **Selected shopping store** — the exact physical store the user explicitly chooses.

When Location Sharing is on, Rung should re-check location appropriately on load/resume/revisit and meaningful movement. GPS may change nearby-store context but must **not silently change the selected shopping store**.

Shopping/Grocery and Copilot must share **one canonical selected-shopping-store state**.

Where Rung claims current local retail data, it must retain exact store/product/package/price/availability context. Generic or estimated data must not masquerade as confirmed exact-store data.

Tax comes from the selected physical store's jurisdiction, not the user's current GPS location.

## Settings boundary

**LOCKED PRODUCT DECISION**

Settings is for controls/defaults, not operational work. Appropriate areas include permissions, security/account controls, notifications, financial defaults, long-term shopping defaults/preferences, Location Sharing ON/OFF, and read-only current-location context.

Normal Settings should not become the place for Find Store, Change Store, Save Location, permanent ZIP maintenance, or operational store selection. Store selection belongs in Shopping and relevant Copilot interactions.

## Product authority hierarchy

**LOCKED PRODUCT DECISION**

1. explicit current request
2. exact favorite
3. exact usual
4. approved substitute when the usual is unavailable
5. household preference/default
6. budget-aware suggestion

A current request outranks stored history. A **Suggested** product does not silently become a favorite/usual. Explicit **Don't care** is real information and is distinct from unanswered.

## Shopping style / Suggested / More Options

**APPROVED DIRECTION**

Household Shopping Style controls how aggressively Rung may trade preferences for savings; exact final labels are not fully locked.

Rung should make reasonable low-input selections and mark them **Suggested**. The user can use **More Options / Change** when desired. Users should not have to confirm every ordinary product individually.

## Budget-first cart / Rebalance

**LOCKED PRODUCT DECISION**

Optimization is whole-cart optimization, not "cheapest item in every category." It respects explicit requests, dietary/hard constraints, exact usuals/favorites, protected items, quantities, recipe requirements, and household preferences.

If user changes push the cart over budget, Rung should offer **Rebalance** rather than silently changing unrelated items. Rebalance previews changes, preserves protected/current choices, starts with flexible items, does not cut required quantity simply to hit budget, and requires user approval. If no valid cart can meet the budget, Rung says so truthfully.

Shopping includes food plus routine household/general items such as detergent, soap, shampoo, toothpaste, paper products, cleaning supplies, pet/baby supplies, batteries, etc.

---

# 6. COPILOT / BANKING MODEL

## Copilot

**LOCKED PRODUCT DECISION**

Architecture:

**deterministic parser/rules** → **LLM only for genuine ambiguity** → **canonical structured action** → **HITL stage/preview** → **confirmation** → **deterministic write/calculation**.

The LLM should not do routine financial math, core product/package logic, cart arithmetic, Safe-to-Spend math, or substitution authority.

Normal users never provide Groq/OpenAI/provider API keys. Provider credentials are server-side and user-facing failures are sanitized.

Consequential actions are staged before commit. Rung must not silently spend money, place orders, change important financial records, or make consequential substitutions outside its authority.

Copilot is intended to work across finance, meals/recipes, shopping, affordability, and Goals, including multiple requested actions in one natural-language interaction with a review/confirm step.

## Plaid/manual mode

**LOCKED PRODUCT DECISION**

Plaid is optional. Rung must remain useful in a manual-first mode and must not pretend to know transactions the user never reported.

"My checking balance is X" means **set/correct the confirmed balance**, then recalculate forward; it must not create a fake expense.

## Finished Shopping / reconciliation

**LOCKED PRODUCT DECISION**

Finished Shopping is the bridge from shopping to financial reality. Show planned total; let the user use planned total or optionally enter the actual receipt total; confirm once; update the financial state and Safe-to-Spend.

If Plaid later sees the same purchase, reconcile/link the records so the financial effect occurs once. Ambiguous transactions must not silently auto-merge.

---

# 7. VERIFIED CURRENT REPOSITORY STATE — 2026-08-18

This section describes what Codex found in the current checkout, not what the product should ultimately do.

## Runtime / persistence

**VERIFIED IMPLEMENTED**

The audited checkout currently resolves `sqlite:///finance.db` to `/home/ky/finance_assistant/instance/finance.db`. Resolution precedence is explicit `RUNG_DB_PATH` → `DATABASE_URL` → legacy `rung_finance.db` fallback.

The code supports PostgreSQL and requires it in beta/production mode, but the audit could not prove that an external deployed PostgreSQL process is currently the active production authority. Do not repeat old "PostgreSQL production cutover complete" claims without fresh runtime evidence.

## Current financial calculations

**VERIFIED IMPLEMENTED + CURRENT LEGACY GAP**

`_compute_safe_to_spend_snapshot()` exists and is browser-visible. It uses checking balance, bills, gas/other commitment, grocery commitment/spend, and protected buffer.

**Pay Yourself First is not implemented in this calculation.**

A second older `compute_liquidity_metrics()` engine is simultaneously active and calculates `safe_disposable_cash`, `food_budget`, grocery remaining, and `free_cash_remaining`. The cart currently defaults to the old `food_budget`, not the intended PYF financial model.

## Shopping intelligence

**VERIFIED IMPLEMENTED**

The repository contains real support for Household Shopping Defaults, Shopping Style, exact product preferences/usuals/favorites, approved substitutions, Suggested products, More Options, budget-first verified-cart optimization, Walmart/Kroger provider/cache foundations, Store×SKU observations, Rung-owned tax, and Finished Shopping.

## Store state

**VERIFIED IMPLEMENTED + CURRENT LEGACY GAP**

Nearby-store discovery and explicit exact-store selection exist. However selected-store state is fragmented across Kroger-named Account fields, retailer settings, request/frontend context, GroceryItem/trip snapshots, and retail identities. The Kroger-named fields may contain Walmart state.

Shopping and Copilot do **not** yet share one complete canonical selected-store entity/state.

## Plaid / financial writes

**VERIFIED IMPLEMENTED**

Plaid Link foundation, encrypted token persistence, sync, manual/Plaid reconciliation, direct balance reconciliation, household-scoped financial writes, and concurrency/idempotency protections exist. Current reconciliation correctness is not all-green; see defects below.

## Current onboarding

**VERIFIED IMPLEMENTED / LEGACY PRODUCT FLOW**

Current onboarding is a three-step modal:

1. Household & Food — household size, favorite proteins, dietary restrictions, allergies.
2. Recurring Bills.
3. Budget Baselines — grocery and fuel baselines.

It does not currently collect the intended new PYF target, checking balance/payday setup, protected buffer, Shopping Style/Household Shopping Defaults, Location Sharing, Plaid/manual choice inside the wizard, or notification preferences.

Auth/session APIs exist, but the audited served SPA did not prove the complete deployment sign-in UI path.

---

# 8. CRITICAL CURRENT PIPELINE MISMATCHES

## Recipes → Active Recipes

**INTENDED:** Recipes → persist active pay-period selection → Active Recipes displays it → Grocery consumes it.

**CURRENT:** Recipe checkboxes and **+ Add to Grocery** only create transient browser selection. They do not persist `MealPlanItem`. **Build Shopping Plan** unions transient IDs with any persisted meal-plan IDs but still does not persist the checked recipes. Copilot is currently the principal real-browser path that persists `MealPlanItem` state. Active Recipes is display-only and unions the transient and persisted states.

**GAP:** normal Recipes activation is not connected to one canonical persisted active-pay-period state.

## Active recipes → modern verified cart

**INTENDED:** persisted active recipes → ingredient requirements → selected-store product resolution → cart.

**CURRENT:** the generic recipe resolver handles recipe aggregation and safe pantry deduction. The modern verified Walmart/Kroger branch is driven primarily by manual unpurchased `GroceryItem` requirements with empty/null `recipe_ids` and **does not currently convert submitted recipe IDs into verified-cart requirements**.

Therefore selected recipes can be ignored by the modern retailer cart path.

Copilot-created recipe-derived GroceryItem snapshots contain recipe IDs, but the verified cart excludes those rows from its active-manual-requirements input.

## Recipe quantities

**CURRENT LEGACY / DEFECT**

Imported ingredients are generally persisted numerically as quantity `1`, unit `item`, even when richer quantities exist in display text. Household size/recipe serving count do not scale the normal UI grocery calculation. Pantry deduction exists only on the generic recipe path.

## Rebalance

**INTENDED:** preview proposed changes → user approves/applies or keeps choices.

**CURRENT:** proper preview/apply APIs exist, but the primary browser Rebalance button currently rebuilds the cart through `buildCart(false)` instead of directly using the preview/apply flow.

## Finished Shopping

**VERIFIED CORE**

The real browser stages then completes Finished Shopping. Completion creates one household-scoped grocery `ExpenseTransaction`, applies one balance delta, records `ShoppingTripCompletion` with planned/actual amount and store snapshot, and refreshes financial state. Safe-to-Spend is recalculated from current state.

## Plaid reconciliation

**CURRENT DEFECT**

Manual/Plaid matching machinery exists, but current date-sensitive tests miss expected proposals for older hard-coded dates. When a candidate is missed, Plaid can create a new imported financial transaction and therefore produce an additional financial effect. Reconciliation must not be called fully safe/green until repaired and reverified.

## Transactions / Bills

**HIGH-PRIORITY DEFECT**

The real browser attaches duplicate submit handlers to Transactions and Bills: inline handlers plus `static/js/transactions.js` handlers. One valid user submit can POST twice. Isolated JS tests do not catch this integrated-browser defect.

---

# 9. CURRENT COPILOT PIPELINE — VERIFIED CORE

Browser text → `/api/copilot/chat` → deterministic parsing plus optional model fallback → staged structured payload → user sees stage dialog → **Save Changes** → `/api/copilot/apply` → shared DB writes + audit.

Verified examples:

- **"I spent $42 on gas"** → staged manual expense → confirmed transaction → balance delta → normal financial state.
- **"My checking balance is $638"** → staged balance reconciliation → direct absolute balance set → no fake expense.
- **"Add milk and dish soap"** → structured manual GroceryItem requirements.
- meal-planning language can persist `MealPlanItem` state.

Current gap: Copilot shopping rows snapshot store name but do not carry one canonical exact store ID/retailer object. Copilot and Shopping therefore share partial store state, not one complete canonical store authority.

---

# 10. CURRENT LOCATION / STORE PIPELINE

**VERIFIED CURRENT BEHAVIOR**

Current UI can obtain device geolocation or use ZIP → `/api/location/nearby-stores` → return exact nearby supported stores → user selects a store → `/api/location/select-store` → persists exact retailer store ID/name plus related account/retailer state.

Discovery itself does not overwrite the selected store.

Current code still contains legacy ZIP/manual location/update behavior and fragmented store fields. This does not fully match the final simplified Settings/location product decision.

---

# 11. TEST / QUALIFICATION STATE

Do not inherit old blanket PASS/beta-ready claims.

The focused Codex audit reported **160 Python tests passed and 9 failed** in the requested critical suites. The failures were six reconciliation tests and three legacy location-update expectation tests.

JavaScript tests passed in the audit run, but those isolated tests did not detect the duplicate real-browser Transactions/Bills submissions.

Historical PostgreSQL/parity/backup/concurrency/deployment claims must be rerun before being treated as current qualification evidence. The audited working tree was also reported as heavily dirty/untracked, so future qualification evidence should record the exact commit/working-tree state.

---

# 12. DATA / SAFETY INVARIANTS

These must be preserved.

- household isolation/scoping;
- authoritative financial write paths;
- idempotency/concurrency protection for consequential actions;
- no duplicate financial effects;
- direct balance reconciliation rather than fake expenses;
- human approval before consequential Copilot/rebalance actions;
- manual-first operation without pretending to know unseen transactions;
- exact-store/retail provenance and truthful uncertainty.

### Destructive DB rule

A prior agent accidentally reset a real SQLite database. `app.testing=True` was not sufficient isolation.

**Standing rule:** never run destructive reset/drop behavior unless an explicit disposable `RUNG_DB_PATH` is established **before app import** and the resolved target is verified non-production. Preserve the repository's fail-closed destructive-operation guard.

---

# 13. APPROVED UI / VISUAL RULES

**LOCKED PRODUCT DECISION**

Use the existing clean/light Rung mockup/reference bundle as the visual authority. Agents do not have permission to independently redesign layout, hierarchy, spacing, typography style, screen structure, component language, or overall style.

Brand:

- product name is **Rung**;
- use the original simple/basic R mark;
- the special R mark is the first letter of **Rung**, not a separate `R Rung` treatment;
- prior staircase/bar and dark-dashboard logo experiments were rejected.

Colors/sidebar imagery may be refined later, but that does not authorize structural redesign.

### Navigation / IA status

The recovered structure **Overview / Copilot / Money / Savings / Goals / Meals / Shopping / Settings** is a **strong draft**, not proven immutable. Bills-under-Money, Can-I-Buy inside Overview/Copilot, and compact mobile navigation are also direction rather than fully locked final IA. Reconcile final IA against the approved visual reference and later explicit owner decisions; do not invent it.

---

# 14. APPROVED BUT NOT YET IMPLEMENTED / NOT PROVEN IMPLEMENTED

Clearly not implemented in the current financial authority:

- PYF target/feasibility engine;
- canonical real-Needs + PYF + buffer + Safe-to-Spend allocation model;
- retirement of the competing old `food_budget` authority.

Approved features whose full implementation status was not established by these audits:

- Goals as the intended full product feature;
- Paycheck Timeline;
- Where Your Money Is Going;
- Ahead/Behind Plan;
- Recurring Spending Watch;
- Payday Recap;
- final Savings/Reserve/Wealth surfaces;
- new onboarding matching the recovered financial/shopping/location model;
- simplified final Settings/location UX;
- one canonical selected-store model;
- full implementation of the approved visual reference across all surfaces.

Verify before changing; do not assume absent or complete without repository evidence.

---

# 15. UNRESOLVED ITEMS — DO NOT INVENT

Financial: exact pay-period boundary rules; Ahead/Behind formula; Timeline math; recurring-spend thresholds; Payday Recap rollover; favorable variance treatment; reserve taxonomy/allocation/redirect rules.

Recipes/meals: exact activation button/copy; removal/deactivation UX; household-size/serving scaling formula; duplicate ingredient aggregation; pantry deduction policy; leftovers; snacks; dedicated manual non-recipe-food UI; whether final IA has a separate Meal Plan screen.

Onboarding: exact screen/question count and order; exact shopping-default questionnaire; notification choices; exact bank-link timing; final review copy.

Navigation: strong draft exists, but final IA is not fully locked.

Pricing/marketing: final pricing is unresolved. Historical one-time/annual/usage-pack figures remain hypotheses unless re-approved.

---

# 16. REJECTED / SUPERSEDED DIRECTIONS

Do not reintroduce without explicit owner approval:

- fixed 50/30/20 or fixed 20% savings;
- Needs as a percentage slider;
- arbitrary max PYF target;
- silently lowering long-term target;
- silently raiding protected savings;
- automatically treating all underspend as more spending money;
- permanent ZIP/Save Location/store-selection work in Settings;
- automatic nearest-store selection or GPS-driven silent store switching;
- cheapest-item-everywhere cart optimization;
- silent Rebalance changes or quantity cuts to fake budget compliance;
- LLM product/package guessing or unsafe conversions;
- estimated/generic retail data presented as exact-store truth;
- normal-user API keys/BYOK;
- LLM routine financial math;
- forcing Plaid;
- fake balancing expenses;
- manual + Plaid double counting;
- Good Steward as product name;
- staircase/bar or dark-dashboard R logo directions;
- separate `R Rung` wordmark;
- agent-led redesign of the approved visual structure.

---

# 17. CURRENT DEVELOPMENT BOUNDARY

Broad recovery/auditing is complete enough to stop. Do not resume the stale milestone numbering as the roadmap.

## Recommended engineering order — **NOT YET A LOCKED PRODUCT DECISION**

1. **Financial-integrity hotfixes:** remove duplicate Transactions/Bills browser submissions; repair/reverify Plaid/manual reconciliation so a missed date window cannot create duplicate financial effects.
2. **Canonical selected-store authority:** one household exact-store state shared by Shopping/Copilot/location/cart/tax; GPS must never auto-change it.
3. **Recipes → Active Recipes → Grocery consolidation:** normal Recipes UI persists active pay-period recipes; modern verified retailer requirements consume recipe-derived needs instead of ignoring recipe IDs; resolve quantity/pantry details only from approved rules.
4. **Rebalance browser wiring:** connect the UI to the existing preview/apply workflow.
5. **Financial authority migration:** implement PYF/real Needs/protected buffer and remove the competing old `food_budget` authority from cart/financial decisions.
6. **Onboarding + Settings alignment:** implement the recovered low-input financial/shopping/location model.
7. **Approved financial intelligence features:** Goals, Timeline, Where Money Is Going, Ahead/Behind, Recurring Spending Watch, Payday Recap, Savings/Reserve/Wealth after unresolved mechanics are explicitly decided.
8. **Full approved visual implementation/acceptance:** use the reference bundle; validate real browser desktop/mobile behavior; no agent-led redesign.

---

# 18. QUICK STATUS MATRIX

| Area | Intended product | Current state | Status |
|---|---|---|---|
| Financial authority | PYF + real Needs + buffer + Safe-to-Spend | Safe-to-Spend + old liquidity/food-budget engines | **Migration required** |
| PYF | user target + per-cycle feasibility | absent | **Decided, not implemented** |
| Recipes activation | Recipes persists active pay-period set | normal UI transient; Copilot persists | **Mismatch** |
| Active Recipes | display, not selection | display-only union of two states | **Underlying state split** |
| Recipe → verified cart | active recipes drive real products | verified branch ignores recipe IDs | **Major gap** |
| Pantry | participates where safe | generic path only | **Partial** |
| Recipe quantity/scaling | quantity/package-aware | import often 1 item; no household scaling | **Incomplete** |
| Product authority | request → favorite → usual → substitute → default → suggestion | modern verified path substantially supports it | **Verified core** |
| Suggested/More Options | low-input, explicit save to learn | implemented | **Verified** |
| Cart optimizer | whole-cart preference-protecting | implemented, uses old food_budget | **Wrong financial authority** |
| Rebalance | preview + approval | APIs exist; main button rebuilds | **Browser mismatch** |
| Finished Shopping | one planned/actual financial effect | wired | **Verified core** |
| Plaid reconciliation | no double count | present; current failures | **Repair/reverify** |
| Balance correction | direct set, no fake expense | implemented | **Verified** |
| Store discovery/selection | GPS discovers; user selects | implemented | **Verified core** |
| Canonical store | one state for Shopping + Copilot | fragmented | **Not complete** |
| Tax | selected-store jurisdiction | owned tax wired | **Verified core** |
| Settings | controls/defaults | legacy operational location paths remain | **Mismatch** |
| Onboarding | new stable-input PYF/buffer/shopping/location setup | legacy 3-step wizard | **Redesign required** |
| Copilot HITL | deterministic-first + stage/confirm | core path implemented | **Verified core** |
| Transactions/Bills | one safe submit | duplicate browser handlers | **High-priority defect** |
| Visual design | approved Rung reference | current app not assumed to match | **Implement/verify** |
| Current DB | production authority must be explicitly proven | checkout uses `instance/finance.db`; deployed authority unknown | **Verify before prod claims** |

---

# 19. RULES FOR FUTURE CHATGPT / CODEX SESSIONS

1. This handoff supersedes the old hallucinating-chat handoff for project-state framing.
2. Owner-approved product decisions outrank assistant suggestions and current legacy behavior.
3. Verify current code/runtime before declaring an implementation complete.
4. For interactive features, automated tests alone do not prove browser correctness; require browser acceptance evidence.
5. Do not use old milestone names/counts as proof of current completion.
6. Do not invent unresolved financial formulas, recipe scaling, reserve mechanics, onboarding sequence, or final IA.
7. Preserve HITL for consequential financial/shopping actions.
8. Preserve database safety, household isolation, idempotency, and no-duplicate-financial-effect rules.
9. Never perform destructive DB work without an explicitly verified disposable `RUNG_DB_PATH` set before app import.
10. Use the approved visual artifacts; do not independently redesign the product.
11. Do not expose provider/BYOK keys to normal customers.
12. GPS does not silently change the selected shopping store.
13. Suggested products do not become learned usuals/favorites unless explicitly saved.
14. Estimates do not masquerade as confirmed exact-store data.
15. When a later owner decision supersedes an older one, record **OLD → NEW** and remove the old rule from authority.

---

# 20. CURRENT NEXT STEP

**Owner review/approval of this canonical handoff.**

After approval, do not run another broad audit. The first recommended Codex implementation task is the narrow **Transactions/Bills duplicate-submit financial-integrity fix**, followed by reconciliation correctness. Then move through the recovered product pipeline one bounded package at a time.

