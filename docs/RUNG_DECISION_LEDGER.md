# Rung Owner Decision Ledger

This file records durable owner decisions. It is not an implementation diary, test report, or substitute for inspecting the current repository.

## Authoritative persisted cart + staged store change

**LOCKED PRODUCT DECISION**

- The cart is backend-owned, household-scoped, and bound to one exact physical store. Requirements describe household needs; cart lines describe resolved store-specific products, packages, availability, provenance, and prices.
- Browser totals and store values are never financial authority.
- Selecting Store B while Store A has an authoritative cart creates a review only. Store A and its cart remain canonical until approval; cancel changes neither.
- Approval atomically promotes the reviewed Store B cart and selected Store B. Rung never silently carries prices across stores, clears a cart, substitutes, or drops unresolved needs.
- Rebalance is a persisted proposal/review/approval flow, not a browser cart mutation.
- Finished Shopping derives only from the approved persisted cart and selected store. A completed cart is immutable historical evidence.

## Authority order

When sources conflict, use this order:

1. Newest explicit owner decision.
2. Current repository and runtime evidence for actual implementation.
3. Approved Rung product and visual authority.
4. Current project-state documents.
5. Historical chats and handoffs.

Later owner decisions supersede older unresolved language. Legacy code describes current implementation, not intended product behavior. Historical PASS claims require evidence tied to the current source, runtime, configuration, and database target.

## Current development priority

1. First, converge the actual served Rung UI with the approved mockups as closely as the real backend state permits.
2. Then test features one at a time.
3. Fix each feature before moving to the next.
4. Do not run a giant all-at-once beta acceptance as the development workflow.
5. Physical geolocation is not automatically the next task.

The first bounded UI-convergence slice is Global Shell + Overview. This priority does not authorize invented data, mock state, backend redesign, or changes to financial authority.

## Runtime roles

- `.venv` is the REAL / ORIGINAL / CURRENT Rung runtime. It is primary evidence for what the owner currently sees. Do not perform destructive or disposable acceptance against its normal data.
- `venv` is the BETA / TESTING Rung runtime. Mutation-capable acceptance is permitted only when its database authority has been explicitly resolved and verified as disposable before application import.
- These are two Python dependency environments executing one shared application source tree. They are not separate Rung implementations. Feature and UI source changes are made once in the repository and are visible to both, subject to dependency, environment, provider, and database differences.
- Every runtime, browser, or test claim must identify the interpreter/runtime used. If both runtimes are checked, report their results separately. Behavior observed only under `venv` is not proof of `.venv` behavior.
- Historical scripts use the two interpreter paths inconsistently. A script name or old PASS report is not sufficient runtime identification; record the exact interpreter and resolved disposable/non-disposable database target.

## Financial product authority

- Rung uses Pay Yourself First, not fixed 50/30/20.
- Needs are real actual or forecast required obligations, not a percentage allocation.
- Protect the checking buffer before calculating Safe-to-Spend.
- Preserve the long-term PYF target when a cycle cannot fully fund it; report temporary feasibility rather than silently lowering the target.
- Never silently raid protected savings.
- Ahead/Behind is informational only. It is not a second Safe-to-Spend engine, and favorable variance does not automatically grant spending permission.
- Current mutable expected-paycheck input must never rewrite historical pay-cycle expectations. Expected-income authority is effective-dated and historical cycles resolve the version effective for that cycle.

## Transaction-deletion authority

- Ordinary unlinked manual or Copilot `ExpenseTransaction` rows may be directly deleted only when deletion reverses their original checking-balance effect exactly once in the same authoritative financial operation.
- A generic Money delete must reject a transaction linked to Finished Shopping, Plaid identity, or any durable reconciliation record/decision. It must not cascade, detach provenance, erase reconciliation history, or create a second economic effect.
- Finished Shopping corrections remain in Shopping/reconciliation authority. Plaid-linked or reconciled rows must be corrected or unmatched through their existing reconciliation authority before they can become eligible; no new unlink workflow is implied.
- Delete eligibility is backend-owned. The served UI may present that authority but must not recreate it from source/category labels.

## Onboarding required-expense authority

- All onboarding fields classified as required belong together on the first onboarding page and must be visibly marked required.
- Users are not required to have expenses.
- “No required expenses” is an explicit reviewed answer, distinct from unanswered/missing setup.

## Recipes and Shopping authority

The intended flow is:

`Recipes -> current pay-period recipe plan -> Active Recipes This Pay Period -> grocery requirements -> user-selected exact physical store -> product resolution -> cart -> Finished Shopping -> reconciliation -> recalculated Safe-to-Spend`

- Recipes is the recipe-selection surface.
- Active Recipes This Pay Period displays canonical pay-period selection; it is not the selection interface.
- An explicit current product request outranks saved preferences.
- Suggested products never silently become favorites or usuals.
- A one-time choice or substitution is not automatically learned.
- Rebalance requires preview, review, and explicit approval before applying changes.
- Optimization respects requirements and preferences; it is not simply cheapest-item selection.

### APPROVED DIRECTION — Blocked Product / Brand Authority

- Household-scoped persistent blocks are negative saved retail preferences and automatic-selection eligibility filters, not allergies or dietary-safety rules.
- Exact retailer product/SKU identity and normalized brand blocks are supported.
- An explicit current request for the blocked exact product or brand overrides the saved block for that request only; the block remains saved and the choice is not learned as favorite/usual.
- A block overrides saved favorite, usual, and approved substitution for automatic selection without deleting those historical records.
- Store Change and Rebalance apply blocks identically to normal product resolution.

### Recipe ownership and legacy provenance

- Rung uses mixed recipe ownership. Trusted Rung catalog recipes are explicit `canonical` rows with no household owner: every household may browse, read, and activate them, but ordinary household users may not edit or delete them.
- User-created and imported recipes are explicit `household_private` rows. The server assigns the current household; only that household may browse, read, edit/delete where served, or activate them. Recipe ingredients inherit the parent recipe's authority.
- Meal-plan activation is always household-scoped. A household may activate canonical recipes or its own private recipes, never another household's private recipe.
- Ambiguous legacy recipes are preserved as explicit `legacy_quarantined` rows. They have no ordinary household owner, are hidden and inert to ordinary recipe/detail/search/ingredient/plan/requirement flows, and are never silently promoted or reassigned. Reclassification requires explicit reviewed administrative or migration authority.
- Historical private-recipe deletion is a tombstone. An owning household must first remove a private recipe from the current authoritative pay-period plan; deletion then preserves the recipe, ingredients, and prior plan identity while excluding the tombstoned row from ordinary current library, search, recommendations, activation, requirements, and shopping. There is no restore workflow in beta. Canonical and quarantined authority is unchanged.

## Location and selected-store authority

These are distinct states:

1. current device location;
2. nearby supported-store discovery;
3. the user-selected exact shopping store.

- GPS may discover stores but never silently selects or changes the shopping store.
- The user controls exact store selection.
- Shopping and Copilot share one canonical selected-store authority.
- Operational store search and selection do not belong in normal Settings. Settings may show location/store context and control Location Sharing.
- Selected-store jurisdiction, not device GPS or legacy account tax fields, governs physical-cart tax.

## Copilot and Plaid authority

- Copilot is deterministic-first: parse -> staged structured action -> user review -> confirmation -> authoritative shared-state write.
- Consequential actions require human approval and retry-safe/idempotent execution.
- A reviewed Copilot draft is bound to its originating household. Copying its operation ID/payload into another household must be rejected rather than becoming a new cross-household operation.
- Plaid is optional. Manual-first Rung must remain useful without it.
- Normal customer Settings must not expose provider/API credentials.

## Database and safety authority

- Preserve household isolation, one economic effect per operation, idempotency, concurrency safety, and human approval.
- Never reset, drop, migrate, import, seed, or otherwise destructively modify a real or historical database to make testing pass.
- Disposable SQLite acceptance that depends on `RUNG_DB_PATH` must set it before importing the application and must verify the resolved target is disposable.
- `app.testing=True` alone is not database isolation.
- PostgreSQL is the canonical beta/production database; SQLite is for local development and explicitly disposable automated/browser acceptance.

## Visual authority

- `docs/visual/rung_visual_reference_bundle/RUNG_VISUAL_PRODUCT_SPEC.md` and approved boards 01–05 are the UI authority.
- Do not redesign the approved layout, hierarchy, spacing, or component language.
- The product is **Rung**. The integrated special R is the first letter of Rung, not a separate `R Rung` mark.
- Current legacy UI is implementation evidence only, not visual authority.
- Real product state must populate approved UI; missing backend behavior must not be faked.

## Recovered late decisions

- Packages 13–17 are no longer product-design “unresolved” items merely because the August 18 handoff called them unresolved. Current code contains their implementations; qualification remains a separate evidence question.
- Expected-paycheck history is effective-dated and immutable by cycle.
- Package 16 is not required to use a background-worker architecture.
- Unknown tax never silently becomes $0.
- Legacy client/account/manual tax rates are not authoritative purchase tax.
- Playwright or emulated geolocation does not prove physical browser/OS permission behavior.
- Historical beta/disposable acceptance performed under `venv` does not establish behavior under the real `.venv` runtime.

## Explicitly unresolved

- Physical-device geolocation permission behavior.
- Nearby-store radius, distance calculation, and ranking behavior.
- Cause of the previously observed Recipes “Browse All” failure in one disposable acceptance environment.
- Final Plaid Link timing during onboarding.
- Self-service registration product/security policy.
- Broader exact local/national tax coverage.
- Remaining legacy compatibility cleanup and final runtime consolidation details.

Do not invent resolutions for these items. Record a new owner decision or current evidence when one becomes available.
