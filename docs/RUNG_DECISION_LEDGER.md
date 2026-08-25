# Rung Owner Decision Ledger

This file records durable owner decisions. It is not an implementation diary, test report, or substitute for inspecting the current repository.

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
