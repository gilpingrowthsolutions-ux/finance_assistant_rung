# Rung Pre-Beta State

## Current state

**OWNER BETA READY TO RESUME.** The repaired owner-beta build is ready for the founder/owner to continue personal beta testing. Owner beta is not complete, and external beta remains blocked until the owner completes it.

## Verified implementation state

- Safe-to-Spend has one canonical, forward-adjusted authority. Global Ahead/Behind has been retired.
- One posted manual or provider income has one canonical income effect and one linked PYF consequence. Pending payroll and balance refresh create no PYF effect; ambiguous income is reviewed rather than guessed.
- One real checking-to-savings movement has one physical economic effect and at most one reviewed PYF fulfillment. Internal allocation is not a physical transfer; transfer match/separate replays are inert.
- Recurring Needs are canonical, current-cycle scoped, and remain manageable even when their explicit Bill occurrence is absent.
- Shopping and Copilot consume canonical forward-adjusted Safe-to-Spend; protected savings and reserves are not silently raided.
- Browser coverage includes factual timeline, Money, recurring management, physical transfer lifecycle/retry/date cases, two-cycle PYF, provider reconciliation, income reconciliation, lower-STS Shopping/Copilot, and all required mobile surfaces.

## Runtime and safety

- `.venv` is the owner runtime; disposable browser acceptance uses `RUNG_ENV=beta` and an explicit `/tmp` `RUNG_DB_PATH` set before application import.
- PostgreSQL remains canonical for beta/production. The protected `rung_finance.db` is historical data and must not be mutated for qualification.
- Durable owner decisions are recorded in `docs/RUNG_DECISION_LEDGER.md`.
