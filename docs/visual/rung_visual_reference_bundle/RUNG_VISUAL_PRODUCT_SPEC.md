# Rung Visual Product Specification

**Status:** Approved working visual specification  
**Product:** Rung  
**Purpose:** Give implementation agents a written interpretation layer for the approved visual references so they do not redesign the product, invent conflicting UX, or mistake illustrative mockup content for product authority.

---

## 1. How to Use This Specification

This document and the five approved visual reference images are one implementation package.

The images define the intended visual language. This document explains which parts are authoritative, which parts are illustrative, and which product rules override anything ambiguous in an image.

Implementation agents must:

- preserve the approved visual structure and component language;
- map the approved design onto the existing working Rung architecture rather than replacing proven backend authority for visual cleanliness;
- treat product and financial behavior described here as higher authority than example numbers or labels shown in a mockup;
- ask or stop when a genuine product decision is missing rather than inventing a new pattern;
- keep changes focused and avoid unrelated redesign/refactoring;
- verify browser-interactive behavior in the real browser. Automated frontend tests are regression protection, not final acceptance.

This is an implementation reference, not permission to rebuild the application from scratch.

---

## 2. Reference Hierarchy

Use the following images together.

### 01 - Master Visual Reference
`01_RUNG_MASTER_VISUAL_REFERENCE.png`

Defines the overall Rung visual direction:

- desktop application shell;
- mobile application feel;
- card language;
- spacing and hierarchy;
- typography direction;
- navigation style;
- high-level screen architecture;
- clean, approachable consumer-finance appearance.

This is the primary visual authority when a supporting board does not show a particular detail.

### 02 - Money, Savings, and Goals
`02_RUNG_MONEY_SAVINGS_GOALS.png`

Defines deeper layouts and component patterns for:

- Money;
- accounts;
- transactions;
- bills and recurring expenses;
- cash flow;
- Savings;
- reserves;
- protected buffer;
- wealth/investments tracking;
- Goals;
- goal progress;
- per-pay-cycle goal planning;
- Copilot-assisted goal affordability.

### 03 - Shopping, Meals, and Copilot
`03_RUNG_SHOPPING_MEALS_COPILOT.png`

Defines deeper layouts and interaction patterns for:

- meal planning;
- recipes;
- preparation/cooking mode entry points;
- shopping lists;
- selected physical store context;
- nearby-store choice;
- cart and budget presentation;
- product choices/substitutions;
- Copilot recommendations and prompt entry.

### 04 - Onboarding, Settings, and Authentication
`04_RUNG_ONBOARDING_SETTINGS_AUTH.png`

Defines visual patterns for:

- first-run onboarding;
- bank/manual setup entry points;
- location permission;
- pay-cycle setup;
- authentication;
- account/security;
- Settings structure;
- notifications;
- shopping preferences.

Important: individual onboarding steps shown in the image are visual examples, not a complete authoritative list of required steps. Section 9 of this document defines the product rules.

### 05 - Shared States and Responsive Behavior
`05_RUNG_SHARED_STATES_RESPONSIVE.png`

Defines cross-application patterns for:

- loading/skeleton states;
- empty states;
- recoverable errors;
- success confirmations;
- destructive confirmations;
- permission/blocked states;
- desktop-to-mobile responsive behavior;
- mobile drawer/more behavior;
- mobile bottom sheets;
- desktop modals;
- banners, toasts, badges, chips, tabs, and progress states.

---

## 3. What Is Visually Locked

Unless the product owner explicitly changes the direction, preserve:

- generous whitespace;
- light, clean surfaces;
- rounded cards with subtle borders/shadows rather than heavy panels;
- strong content hierarchy;
- simple modern consumer-finance presentation;
- restrained use of accent color;
- clear primary actions;
- compact secondary/tertiary controls;
- consistent icon treatment;
- readable charts and progress indicators;
- desktop sidebar + focused main content area;
- mobile-first interaction patterns for shopping and day-to-day use;
- consistent component behavior across sections.

Do not redesign each screen independently. Rung should look like one product.

---

## 4. Branding Is Temporarily Flexible

The current mockups are approved for structure and visual language, but three branding details are **not final**:

1. exact Rung logo treatment;
2. exact color palette;
3. decorative mountain/sidebar artwork.

These can be refined later without changing the approved layout, hierarchy, spacing, navigation structure, or component language.

The current green palette is a working palette, not a requirement to make Rung visually resemble another finance or consumer brand.

When branding changes later, prefer token/theme changes over screen redesigns.

---

## 5. Retailer Logos Are Mockup Placeholders

Retailer logos shown in visual references communicate store identity during design work. Production UI must not depend on third-party retailer logos for cohesion.

The production store component must still work cleanly with:

- a neutral store icon;
- retailer/store name in text;
- physical-store location information;
- optional retailer branding only when its use has been approved for production.

Never imply retailer sponsorship, endorsement, or partnership merely because Rung can retrieve retailer data.

---

## 6. Primary Information Architecture

Desktop top-level product structure:

- Overview
- Copilot
- Money
- Savings
- Goals
- Meals
- Shopping
- Settings

`Can I Buy?` is not intended to remain a primary top-level destination. Its functionality belongs prominently on Overview and naturally inside Copilot.

Bills belong under Money.

### Mobile

The intended compact mobile structure is approximately:

- Home
- Copilot
- Money
- Shop
- More

`More` can contain lower-frequency destinations such as Savings, Goals, Meals, and Settings.

Supporting visual boards may show simplified mobile navigation for composition. Implement the shared navigation architecture rather than blindly reproducing every thumbnail label.

---

## 7. Overview

Overview is the user's financial snapshot and everyday decision surface.

It should prominently communicate:

- Safe-to-Spend until payday;
- what is already protected;
- upcoming required spending;
- current pay-cycle status;
- whether the user is ahead/behind plan;
- useful access to current balance updating;
- quick access to Copilot / affordability guidance.

The page should answer, quickly:

**What is protected, what still has to be covered, and what can I safely spend?**

Example dollar amounts shown in mockups are illustrative only.

---

## 8. Financial Model: Product Rules Override Mockup Labels

Rung follows **Pay Yourself First**.

The previous fixed 50/30/20 three-slider concept is obsolete and must not be reintroduced even if an illustrative mockup contains ratio-like graphics.

Conceptual order:

`Money available -> desired Pay Yourself First contribution -> actual/forecast Needs -> protected checking buffer -> Safe-to-Spend/Wants`

### Pay Yourself First

- User chooses a long-term target percentage.
- It is adjustable.
- It is not fixed at 20%.
- There is no arbitrary maximum imposed merely for UI convenience.

If the target is too aggressive for the current pay cycle:

- do not silently change the long-term target;
- calculate the temporary feasible contribution;
- explain that the target is aggressive for this cycle;
- recommend the feasible contribution for this cycle;
- preserve the user's long-term target.

### Needs

Needs are actual or forecast required life expenses, not a fixed percentage. Examples include bills, groceries, required transportation, prescriptions, childcare, household necessities, and other genuinely required spending.

### Safe-to-Spend

Safe-to-Spend is what remains safely available after protections and required obligations.

Do not automatically convert every underspend into permission to spend more. Being ahead of plan should remain meaningful.

### Protected Savings

If a genuinely necessary unexpected expense exceeds available Safe-to-Spend, Rung must ask before using protected savings rather than silently raiding it.

---

## 9. Savings, Reserves, Wealth, and Goals

### Savings

Savings is financial protection and wealth infrastructure, not one generic bucket.

Possible reserve types include:

- Emergency Reserve;
- Vehicle Repair Reserve;
- Home / Appliance Reserve;
- Medical Reserve;
- custom reserves.

Individual reserves may have target balances. When one is sufficiently funded, future contributions may be redirected elsewhere rather than growing every reserve forever.

### Wealth / Investments

Rung may track:

- long-term cash waiting to invest;
- investment balances;
- contributions and current value.

Moving money from long-term cash into investments is a transfer of wealth composition, **not an expense**.

Rung is not initially a robo-adviser. Do not turn these screens into security-picking or automated-trading UX.

### Goals

Goals are user-chosen objectives, distinct from reserves.

Examples include vacation, Christmas, vehicle purchase, computer, down payment, debt payoff, and wedding.

A goal may show:

- target amount;
- target date;
- amount saved;
- progress;
- recommended contribution per pay cycle.

Goal affordability should be available through Copilot.

---

## 10. Location and Store Selection: Critical Separation

These are three different states:

1. **Current Device Location** - Where am I now?
2. **Nearby Store Discovery** - Which supported physical stores are nearby?
3. **Selected Shopping Store** - Which exact physical store am I shopping from?

Required pipeline:

`Current Device Location -> discover nearby exact supported stores -> user chooses exact physical store -> canonical selected shopping store -> Shopping + Copilot use that store -> products/prices/availability/tax/cart`

### Hard Rules

- Location may update automatically when sharing is enabled.
- GPS/location must **not** silently change the selected shopping store.
- The exact shopping store remains user-controlled.
- Copilot and Shopping use one canonical selected-store authority.
- Store selection belongs in Shopping and contextually in Copilot, not as an operational Settings workflow.
- Current device location may be shown read-only in Settings.
- Do not require routine ZIP-code maintenance when device location is available.
- Do not promise continuous background tracking while the app is closed.

### Tax

For a real retail cart, the selected physical store location is authoritative for purchase tax. Device GPS must not override the store's tax jurisdiction.

---

## 11. Shopping and Retail

Rung shopping includes groceries **and** household/general necessities.

The product experience is store-specific and should support:

- selected exact physical store;
- current product price;
- package/variant;
- availability;
- preferences/usuals;
- approved substitutions;
- alternative options;
- shopping budget;
- cart total including appropriate tax context;
- Finished Shopping / actual-spend reconciliation.

Never present generic web results or estimates as confirmed local inventory/pricing.

The UI may distinguish states such as:

- Suggested;
- Usual/Favorite;
- alternative;
- choice required;
- unavailable;
- estimate versus confirmed local data.

Make uncertainty clear without making the interface feel like an admin tool.

---

## 12. Meals

Meals should connect family meal planning to financial reality rather than behave as a disconnected recipe app.

The experience may include:

- weekly meal plan;
- recipes;
- planned meal cost;
- grocery impact;
- pantry-aware planning where supported;
- adding required ingredients to Shopping;
- cooking/Prepare Mode.

The deeper product loop is:

`meal plan -> requirements -> preferred products -> selected local store -> cart -> actual shopping -> financial reconciliation`

---

## 13. Copilot

Copilot is Rung's cross-product financial assistant, not merely a chat screen.

It should be able to help users reason across money, savings, goals, meals, and shopping.

Important use cases include:

- Can I afford this?
- Can I afford this goal?
- What should I adjust this pay cycle?
- Help feed my household within the available budget.
- Help with shopping at the currently selected physical store.
- Explain why Safe-to-Spend changed.

Recommendations should feel actionable and grounded in the user's actual Rung state.

Do not let Copilot silently bypass financial protections or store-selection authority.

---

## 14. Onboarding

Onboarding should feel like setting up a polished consumer device/app: short, guided, trustworthy, and progressive.

Collect stable information that Rung cannot safely determine automatically.

The complete product direction includes, as needed:

- welcome / core promise;
- linked bank versus manual setup;
- current balance when manual setup is used;
- payday/pay schedule;
- Pay Yourself First target percentage;
- protected buffer;
- minimal household basics;
- shopping preferences;
- location-sharing permission;
- notifications;
- review / ready state.

Rung already contains onboarding authority for household/food preferences, recurring bills, and grocery/fuel baselines. Extend/consolidate working backend authority rather than discarding it merely to match a mockup.

### Store Selection During Onboarding

A visual reference may show store selection during setup because it demonstrates the interaction pattern. Treat initial store choice as optional/convenient setup only if the final product flow supports it.

The permanent rule remains: current device location discovers nearby stores; the user explicitly chooses the shopping store; later GPS movement does not silently replace that selection.

---

## 15. Settings

Principle:

**Settings = controls and defaults, not work.**

Settings should feel like a normal consumer Settings page.

Appropriate content includes:

- Pay Yourself First target;
- protected buffer;
- Location Sharing toggle;
- read-only current location;
- long-term shopping preferences;
- notifications;
- account/security;
- bank connection management when appropriate;
- Advanced as a collapsed area where genuinely necessary.

Settings should not normally contain:

- operational store search/selection;
- ZIP maintenance;
- manual tax-rate editing;
- grocery/cart work;
- provider/API configuration;
- internal debug controls.

Current balance is financial **state**, not a preference. Preferred entry point: Overview -> Current Balance -> Update Balance.

---

## 16. Authentication and Security UX

Rung needs normal consumer-facing authentication states, including:

- sign in;
- create account;
- log out;
- session-expired handling;
- account identity/security management;
- connection/security guidance without exposing internal secrets.

The visual design should communicate trust without implying guarantees beyond the actual implementation.

Do not expose API keys, database credentials, provider secrets, or private configuration in consumer UI.

---

## 17. Responsive Behavior

Desktop and mobile are the same product, not separate designs.

### Desktop

- persistent sidebar is appropriate;
- content can use multi-column cards/tables where readable;
- dialogs may use centered modals.

### Mobile

- prioritize one-handed, in-store use;
- avoid preserving the desktop sidebar at mobile widths;
- use compact bottom navigation plus More/drawer patterns;
- transform dense tables into cards/lists;
- use bottom sheets for contextual selection/actions where appropriate;
- keep primary actions reachable and obvious.

Do not simply shrink desktop layouts.

---

## 18. Shared States

Every major surface should have deliberate states for:

### Loading
Use skeletons/placeholders that preserve the eventual layout and reduce visual jumping.

### Empty
Explain what is missing and provide one obvious next action. Avoid dead blank screens.

### Recoverable Error
Explain the user-facing problem and provide useful recovery such as Retry, reconnect, use another source, or return to a safe state.

### Success
Confirm meaningful writes/actions without excessive celebration or modal interruption.

### Destructive Action
Use explicit confirmation for actions such as deletion, removal, disconnecting an important account, or other consequential operations.

### Permission / Blocked State
Explain why permission or setup is needed and provide the correct next action. Do not disguise unavailable functionality as working.

---

## 19. Existing Architecture Must Be Respected

Implementation work should preserve proven authoritative services and migrate gradually.

Important current facts:

- authoritative checking balance is persisted in `Account.checking_balance`;
- concurrency-aware balance writes are centralized in `services/financial_state.py`;
- use `get_household_account()`, `apply_balance_delta()`, and `set_balance_absolute()` instead of creating new balance-mutation paths;
- current financial calculations contain parallel/older concepts that must eventually be consolidated into the locked Pay Yourself First model rather than cosmetically re-skinned;
- current store state is split and needs eventual canonical selected-store consolidation;
- working retailer/provider/cache/tax/preference behavior must not be ripped out merely for architectural neatness.

---

## 20. Frontend Runtime Warning

This is a major implementation constraint.

The production browser currently does not always use the same JavaScript implementation exercised by automated frontend tests.

Known example:

- `static/js/grocery.js` contains `setupGroceryInit()`;
- production initialization does not use that as the primary grocery controller;
- the real browser cart path uses separate inline `buildCart()` logic in `templates/index.html`.

Transactions/Bills also contain duplicate handler paths.

Before trusting large UI implementation, move toward **one real frontend runtime** so the code tested automatically is the code actually used by the browser.

Do not declare interactive UI PASS only because automated tests passed.

---

## 21. Implementation Order

The approved product direction does not require another architecture-gate process.

Recommended implementation sequence:

1. one real frontend runtime;
2. canonical live device-location state;
3. nearby supported-store discovery;
4. canonical user-selected shopping store shared by Shopping + Copilot;
5. approved onboarding experience;
6. simplified consumer Settings;
7. Overview/current-balance UX through financial-state authority;
8. financial consolidation into Pay Yourself First -> Needs -> buffer -> Safe-to-Spend;
9. Shopping/cart runtime consolidation while preserving proven retail infrastructure;
10. gradual legacy cleanup only after replacements work.

Use the approved visual references while implementing each area rather than letting an agent create a new design ad hoc.

---

## 22. Browser Acceptance Rule

For browser-interactive features such as:

- GPS/location;
- nearby-store discovery;
- selected store;
- forms;
- onboarding;
- cart interactions;
- responsive navigation;
- modals/bottom sheets;
- Finished Shopping;

**Automated tests do not equal final PASS.**

Final acceptance requires real-browser confirmation by the product owner or an explicitly approved browser acceptance run.

---

## 23. Production Data Safety

Never drop, reset, delete, or import over the real Rung database unless the target is explicitly verified as disposable.

Protected historical SQLite:

`~/finance_assistant/rung_finance.db`

Production PostgreSQL:

`rung_prod`

Do not expose or request secrets in implementation reports.

---

## 24. Do Not Infer / Do Not Redesign

When something is unclear, implementation agents must **not** use the ambiguity as permission to redesign the product.

Do not:

- rename Rung;
- return to Good Steward;
- resurrect fixed 50/30/20 sliders;
- treat mockup financial numbers as formulas;
- make Needs an arbitrary percentage;
- make store selection a Settings workflow;
- make GPS automatically select/change the shopping store;
- silently use protected savings;
- make retailer logos a required component dependency;
- expose manual tax editing in normal Settings;
- turn investing into stock-picking/trading advice;
- add another architecture gate;
- trust inactive/test-only frontend code as proof the browser works;
- perform broad refactors simply to make code resemble the mockups;
- replace working provider/tax/financial authority for visual cleanliness;
- invent API keys, secrets, configuration, or live retailer certainty;
- change the approved layout/component language because branding will change later.

If a visual detail conflicts with a locked product rule, **the product rule wins**.

---

## 25. Definition of Visual Implementation PASS

A screen is visually implemented well when:

- it clearly belongs to the approved Rung design family;
- desktop and mobile behavior match the approved interaction patterns;
- content hierarchy and primary action are obvious;
- the implementation uses actual product state rather than hard-coded mockup assumptions;
- loading, empty, error, and confirmation states are handled where relevant;
- the real browser exercises the same intended runtime logic;
- existing financial/store/security authority is respected;
- no unrelated redesign was introduced;
- branding details marked temporary remain swappable without structural redesign.

---

## 26. Guidance for Codex Sessions

Use this spec and only the relevant reference image(s) for each focused implementation task.

Preferred instruction style:

> Inspect as much as necessary, but keep final reports concise and decision-relevant. Do not narrate exploration or dump long code excerpts.

For each task:

1. inspect current implementation and callers;
2. identify the smallest safe change that brings the screen toward the approved visual/product behavior;
3. preserve backend authority;
4. remove or consolidate duplicate frontend runtime paths only when in scope and safely tested;
5. run focused automated tests;
6. report PASS / NOT PASS;
7. identify real-browser acceptance steps separately.

The goal is not to make the codebase aesthetically perfect. The goal is to make the **actual Rung product** match the approved experience safely and incrementally.
