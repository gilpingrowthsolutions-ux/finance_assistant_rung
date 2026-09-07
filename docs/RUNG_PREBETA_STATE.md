# Rung Pre-Beta State

## Current / next state

**CURRENT — OWNER BETA IN PROGRESS.** Owner physical current-location acceptance and the owner-approved Overview layout repair are complete; owner beta remains open for its remaining acceptance work.

**NEXT — continue the remaining owner-beta acceptance.** External closed beta remains blocked until owner beta is completed and any newly found blockers are addressed.

## Verified implemented

- Safe-to-Spend is the singular forward-adjusted spending authority; global Ahead/Behind is retired.
- Pay Yourself First is canonical: actual income produces one income effect and one linked PYF consequence; balance refresh and pending payroll produce zero PYF. Manual/provider income reconciliation is reviewed, and ambiguous income is not guessed.
- A physical savings transfer has one economic effect; internal allocation is distinct. Reconciliation supports Match / Keep Separate and replay-safe outcomes.
- Required recurring obligations are household-scoped canonical authority, separate from explicit Bill occurrences. Explicit and projected occurrences are deduplicated; projections feed forward Needs through the later of 31 calendar days from as-of or the payday after next payday. Exact-payday income precedes same-day Needs.
- The forward reserve protects the minimum current funds needed for known future required cash. Impossible Needs floor Safe-to-Spend at $0 and report a truthful shortfall.
- Shopping's ceiling and Copilot's affordability/consumption use canonical Safe-to-Spend. The selected Shopping store remains user-controlled; protected savings, reserves, and buffer are not silently raided.
- Household isolation, approval, idempotency, and one-economic-effect discipline remain required authorities.
- Live physical current-location owner acceptance is complete.
- The duplicate Overview Copilot content CTA is removed; the compact top-right CTA remains.
- The Checking Balance row's label and update context render separately.
- The misleading green Safe-to-Spend pseudo-gauge fragment is removed. The arch is intentionally a neutral, non-quantitative frame.
- The Safe-to-Spend hero copy no longer overlaps the neutral frame. Overview uses one cohesive shared two-column desktop grid: This Pay Cycle aligns with Safe-to-Spend, and Last Pay Cycle aligns beneath Your money is protected. The single Overview Copilot CTA and single Overview balance action remain intact.

## Decided but not implemented

- Complete approved-visual-reference convergence across remaining surfaces is still pending; use the approved visual specification and boards, never an agent-led redesign.
- A future quantitative Safe-to-Spend gauge requires an explicitly approved semantic before implementation.

## Legacy / to be replaced

- `rung_finance.db` is protected historical SQLite data; a controlled PostgreSQL/data-adoption path remains unapproved. PostgreSQL is the beta/production authority.
- Compatibility-only liquidity/financial fields and functions, legacy location/store mirrors and provider paths, and the hybrid static-module/large-inline-controller frontend remain present.

## Known defect

- No unresolved automated location-truthfulness defect is carried.

## Unresolved

- Nearby-store radius, distance, and ranking behavior.
- Final Plaid Link timing during onboarding; self-service registration product/security policy (currently deliberately unavailable, not implemented).
- Broader exact local/national tax coverage; exact historical Safe-to-Spend change attribution; canonical affordability for an uncreated dated Goal.
- Cause of the prior disposable-environment Recipes “Browse All” failure; remaining legacy cleanup and frontend-runtime consolidation details.

## Canonical authorities

- [Architecture Contract](RUNG_ARCHITECTURE_CONTRACT.md), [Decision Ledger](RUNG_DECISION_LEDGER.md), [Canonical Handoff](../RUNG_CANONICAL_HANDOFF_2026-08-18.md), and the [approved visual specification and boards](visual/rung_visual_reference_bundle/RUNG_VISUAL_PRODUCT_SPEC.md) govern product intent.
- Repository/runtime evidence controls what is actually implemented.
