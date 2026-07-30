// !!! WARNING — DO NOT MOVE THIS <script> TO <body>, AND DO NOT ADD `defer`
// !!! OR `async`. The cross-tab listener below references `prepareState`,
// !!! `renderPrepareStep`, and `refreshRecipes`, which are defined LATER
// !!! in the main inline body script. Today this works because the
// !!! references are resolved at FIRE-time (inside the storage event
// !!! callback), not at LOAD-time, and the listener never fires until
// !!! another tab writes to PREPARE_STORAGE_KEY. See the detailed
// !!! explanation inside the JSDoc block below.

/**
 * Prepare-Mode Progress Storage Helpers
 * =====================================
 *
 * Standalone module extracted from templates/index.html so the on-disk
 * shape contract + migration ladder are testable in isolation. Loaded
 * via <script src="/static/js/prepare-storage.js"></script> in <head>
 * AFTER the Plaid SDK script.
 *
 * Public API (callable as globals from the main inline script):
 *   - PREPARE_STORAGE_KEY     localStorage key for prepare progress.
 *   - PREPARE_SCHEMA_VERSION  current on-disk schema version.
 *   - _loadPrepareProgress    @public — single-entry lookup.
 *   - _savePrepareProgress    @public — persist one entry.
 *   - _clearPrepareProgress   @public — remove one entry.
 *   - _wrapPrepareMap         @private — shape normalizer.
 *   - _loadPrepareMap         @private — safe-parse + migration.
 *   - _savePrepareMap         @private — write-through.
 *
 * Also registers a cross-tab `storage` event listener that mutates the
 * open Prepare-mode dialog when another tab writes to PREPARE_STORAGE_KEY.
 * See the plain `// !!! WARNING` comment at the top of this file for
 * the script-tag ordering invariant; the listener body references
 * `prepareState`, `renderPrepareStep`, and `refreshRecipes` from the
 * main inline body script, but only at FIRE-time (inside the event
 * callback), which is why synchronous `<head>` placement is safe.
 *
 * Node.js testability: the conditional `module.exports` block at the
 * bottom exports the helpers for isolation testing. The browser ignores
 * that block (no `module` global in browser globals).
 */

'use strict';

//   { step: <int 0..totalSteps-1>, totalSteps: <int>, savedAt: <ISO> }
// Stale handling: on reopen, if saved.step is missing/non-int OR falls
// outside the freshly-parsed step count, we start at 0 and overwrite
// the stale entry. Finish clears the entry; X/Esc retain it so the
// user can come back later.
/**
 * Prepare-mode progress persistence (localStorage-backed).
 *
 * ON-DISK SHAPE CONTRACT (v1):
 *   {
 *     v: 1,
 *     entries: {
 *       "<recipeId>": { step: <int>, totalSteps: <int>, savedAt: <ISO> },
 *       ...
 *     }
 *   }
 *
 * MIGRATION LADDER:
 *   v0 (legacy, pre-versioning):
 *     { "<recipeId>": {...} } — a bare inner map, no top-level `v`.
 *     `_loadPrepareMap` migrates a v0 payload to v1 in memory; the
 *     next `_savePrepareMap` call writes it back as v1 (write-
 *     through migration — minimal disruption, single source of
 *     truth for the contract).
 *   v1 (current): the wrapped shape above. Reads pass through;
 *     saves are normalized via `_wrapPrepareMap`.
 *   v2+ (future): bump `PREPARE_SCHEMA_VERSION` and extend the
 *     strict-v1 branch in `_loadPrepareMap` to migrate older
 *     versions incrementally. New shape variants should be added
 *     as sub-objects under their own key (e.g. `entries: { v: 2,
 *     ... }`) so the outer `{v, entries}` envelope is preserved.
 *     Alternatively, switch to a tagged-union `kind` field at the
 *     top level if the shape diverges substantially.
 *
 * ROLE OF EACH HELPER:
 *   `_wrapPrepareMap`  — Normalize ANY caller-passed shape into v1.
 *                        Logs `console.warn` for non-empty bare
 *                        maps (legacy migration) and nested-save
 *                        situations (already-wrapped but entries
 *                        is malformed).
 *   `_loadPrepareMap`  — Read + parse with schema-versioning + safe
 *                        fallback. Returns `{v: 1, entries: {}}`
 *                        on read failure or malformed v1.
 *   `_savePrepareMap`  — Write the v1 shape via `_wrapPrepareMap`.
 *                        Tolerates quota / private-mode failures.
 *   `_loadPrepareProgress(recipeId)`
 *                      — Lookup a single entry via
 *                        `.entries[String(recipeId)]`.
 *   `_savePrepareProgress(recipeId, step, totalSteps)`
 *                      — Persist one entry; ISO timestamp added.
 *   `_clearPrepareProgress(recipeId)`
 *                      — Drop one entry from the inner map.
 *
 * KEY: `PREPARE_STORAGE_KEY` (constant below). Audit protocol
 * for accidental bypass:
 *   `grep -n "setItem(PREPARE_STORAGE_KEY" templates/index.html`
 *   Only callers through this module should write to that key.
 */
const PREPARE_STORAGE_KEY = 'rung_prepare_progress_v1';
const PREPARE_SCHEMA_VERSION = 1;

// On-disk shape (v1):
//   { v: 1, entries: { [recipeId]: { step, totalSteps, savedAt } } }
// Legacy (v0) payloads were a bare map:
//   { [recipeId]: { step, totalSteps, savedAt } }
// _loadPrepareMap migrates a v0 payload to v1 in memory; the next
// _savePrepareMap call writes it back as v1 (write-through migration
// — minimal disruption, single source of truth for the contract).

// Single shape-normalization point. Accepts either a wrapped v1
// object or a legacy bare map and returns the wrapped v1 form. Used
// internally by _savePrepareMap so future contributors cannot
// accidentally bypass the shape contract.
/**
 * @private
 * Internal shape normalizer for `_savePrepareMap`. Accepts a wrapped
 * v1 object, a legacy bare inner map, or a defensive `{}` / null /
 * array fallback, and always returns the wrapped v1 form. Logs a
 * `console.warn` for non-empty bare maps (legacy migration signal)
 * and for nested saves where `v` is set but `entries` is malformed
 * (data-preservation fallback).
 * @param {object|null|Array} input — caller-provided value to wrap.
 * @returns {{v: number, entries: object}} — always the v1 shape.
 */
function _wrapPrepareMap(input) {
  if (input && typeof input === 'object' && !Array.isArray(input)
      && input.v === PREPARE_SCHEMA_VERSION
      && input.entries && typeof input.entries === 'object'
      && !Array.isArray(input.entries)) {
    return input;
  }
  // Caller passed something other than a strict-v1 wrapped object.
  // Distinguish two diagnostic categories via the absent-vs-present
  // `v` header so operators can tell a legitimate v0 migration from
  // an unexpected malformed-v1 path. Empty / null / array fallbacks
  // stay silent — those are the legitimate "clear" + "defensive"
  // paths and would spam the console during normal operation.
  let inner = {};
  if (input && typeof input === 'object' && !Array.isArray(input)) {
    inner = input;
    const keyCount = Object.keys(input).length;
    if (keyCount > 0) {
      // input.v === undefined  → legacy v0 inner-map migration.
      // Fires exactly once per page-load for users with old data on
      // disk; subsequent saves land in strict-v1 land.
      if (input.v === undefined) {
        console.warn(
          `[rung] _wrapPrepareMap: wrapping legacy v0 inner-map as v1 (entries=${keyCount}). ` +
          `On-disk shape is being migrated this session; future saves will write the wrapped v1 shape.`
        );
      } else {
        // input.v !== undefined  → malformed v1 — already wrapped but
        // entries is missing/invalid. Persist as-is to avoid data
        // loss; flag for operators.
        console.warn(
          `[rung] _wrapPrepareMap: nested save detected — input has v=${String(input.v)} but is missing a valid entries field (entries=${keyCount}). ` +
          `Persisting as-is to avoid data loss; please inspect ${PREPARE_STORAGE_KEY} in DevTools.`
        );
      }
    }
  }
  return { v: PREPARE_SCHEMA_VERSION, entries: inner };
}

/**
 * @private
 * Reads `PREPARE_STORAGE_KEY` and parses with schema-versioning.
 * Returns the wrapped v1 form, or empty v1 on any read failure,
 * missing key, legacy v0 (migrated in memory), top-level array, or
 * malformed v1 (`v=1` with invalid `entries`, reset to empty).
 * @returns {{v: number, entries: object}}
 */
function _loadPrepareMap() {
  try {
    const raw = localStorage.getItem(PREPARE_STORAGE_KEY);
    if (!raw) return { v: PREPARE_SCHEMA_VERSION, entries: {} };
    const obj = JSON.parse(raw);
    const isV1Shape = obj && typeof obj === 'object' && !Array.isArray(obj)
        && obj.v === PREPARE_SCHEMA_VERSION
        && obj.entries && typeof obj.entries === 'object'
        && !Array.isArray(obj.entries);
    // Current schema (v1): top-level v + entries, both plain objects.
    if (isV1Shape) return obj;
    const hasVHeader = obj && typeof obj === 'object' && !Array.isArray(obj)
        && obj.v === PREPARE_SCHEMA_VERSION;
    // Malformed v1 (v matches but entries is a string / array / null /
    // missing). The user at some point had v1 stored but it was edited
    // by hand, partially migrated, or otherwise corrupted. Reset to
    // empty v1 rather than double-nesting the malformed object as
    // legacy entries (which would accumulate `{v: 1, entries: {v: 1,
    // ...}}` on every subsequent save).
    if (hasVHeader) return { v: PREPARE_SCHEMA_VERSION, entries: {} };
    // Legacy unversioned payload: the entire top-level object IS
    // the entries map. Migrate in memory; we'll write back as v1
    // on the next _savePrepareMap call.
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      return { v: PREPARE_SCHEMA_VERSION, entries: obj };
    }
    return { v: PREPARE_SCHEMA_VERSION, entries: {} };
  } catch (e) {
    return { v: PREPARE_SCHEMA_VERSION, entries: {} };
  }
}

/**
 * @private
 * Writes via `_wrapPrepareMap` so disk always lands in v1 shape.
 * Tolerates quota / private-mode failures silently (the in-memory
 * prepare state still works for the current session).
 * @param {object} map — wrapped v1 OR legacy inner map (legacy is
 *   auto-wrapped on save).
 */
function _savePrepareMap(map) {
  try {
    // Always write the wrapped v1 shape on disk, even if a caller
    // passes a bare inner map. _wrapPrepareMap is the single point
    // where the contract is enforced.
    localStorage.setItem(
      PREPARE_STORAGE_KEY,
      JSON.stringify(_wrapPrepareMap(map))
    );
  } catch (e) {
    // Storage disabled / quota exceeded. Silently degrade; the
    // in-memory prepare state still works for the current session.
  }
}

/**
 * @public
 * Single-entry lookup. Consumed by the Prepare UI: chip render via
 * `refreshRecipes`, and step validation in the open/dialog flow.
 *
 * SEMANTIC GUARANTEES:
 *   - Returns `null` when `recipeId` is null/undefined (defensive).
 *   - Returns `null` when no entry exists for the given recipeId
 *     (i.e., the user has never opened Prepare-mode for this recipe,
 *     or has finished it and the entry was cleared).
 *   - Reads always go through the canonical safe-parse path
 *     (`_loadPrepareMap`), so the consumer can trust the shape and
 *     never sees a malformed or legacy payload.
 *   - `recipeId` is coerced via `String(recipeId)` before lookup, so
 *     integer and string keys behave identically (pass either freely).
 *
 * @param {string|number} recipeId
 * @returns {step: number, totalSteps: number, savedAt: string}|null}
 *   `null` when the entry is missing or recipeId is null/undefined.
 */
function _loadPrepareProgress(recipeId) {
  if (recipeId == null) return null;
  const entry = _loadPrepareMap().entries[String(recipeId)];
  if (!entry || typeof entry !== 'object') return null;
  return entry;
}

/**
 * @public
 * Persists one entry. Clamps `step` / `totalSteps` to safe integer
 * ranges and adds an ISO `savedAt` timestamp before writing.
 *
 * SEMANTIC GUARANTEES:
 *   - Silently no-ops when `recipeId` is null/undefined (defensive).
 *   - Adds a fresh `savedAt = new Date().toISOString()` on every save,
 *     so the chip's "Resume at step N" age can be derived from this
 *     field by consumers (e.g., "saved 2 min ago").
 *   - Mutates ONLY the entry for the given recipeId; other entries
 *     in the inner map are preserved untouched.
 *   - Persistence is write-through: if the on-disk payload is legacy
 *     v0, the next save normalizes it to v1 via `_wrapPrepareMap`.
 *
 * @param {string|number} recipeId
 * @param {number} step — current step index, clamped to `>= 0`.
 * @param {number} totalSteps — total step count, clamped to `>= 1`.
 * @returns {void}
 */
function _savePrepareProgress(recipeId, step, totalSteps) {
  if (recipeId == null) return;
  const wrapped = _loadPrepareMap();
  wrapped.entries[String(recipeId)] = {
    step: Math.max(0, Math.floor(Number(step) || 0)),
    totalSteps: Math.max(1, Math.floor(Number(totalSteps) || 1)),
    savedAt: new Date().toISOString(),
  };
  _savePrepareMap(wrapped);
}

/**
 * @public
 * Removes one entry from the inner map.
 *
 * SEMANTIC GUARANTEES:
 *   - Silently no-ops when `recipeId` is null/undefined (defensive).
 *   - `recipeId` is coerced via `String(recipeId)` before lookup, so
 *     integer and string keys behave identically (pass either freely).
 *   - Skips the disk write entirely when the entry didn't already
 *     exist; `PREPARE_STORAGE_KEY` is NOT touched, so the cross-tab
 *     `storage` event will NOT fire spuriously from a no-op clear.
 *   - When the entry DID exist, the entry is removed and the wrapped
 *     v1 map is rewritten via `_savePrepareMap` — the cross-tab
 *     `storage` event WILL fire (positive observation: clearing an
 *     existing entry propagates to other open tabs).
 *
 * @param {string|number} recipeId
 * @returns {void}
 */
function _clearPrepareProgress(recipeId) {
  if (recipeId == null) return;
  const wrapped = _loadPrepareMap();
  if (delete wrapped.entries[String(recipeId)]) {
    _savePrepareMap(wrapped);
  }
}

// Cross-tab prepare step sync. Per W3C, `storage` does NOT fire
// in the originating tab — so we just mirror whatever the OTHER
// tab wrote without an "is this my own write" guard. Two open
// Rung tabs cooking the same recipe stay in lock-step: Advancing
// in one advances the other; the Resume chip re-renders so the
// saved step is reflected without a reload.
//
// Branches:
//  - source tab ADV/PREV → mirror step on matching open dialog.
//  - source tab FINISH (cleared entry by `_clearPrepareProgress`)
//    → leave OUR dialog open; only the chip freshness updates.
//    We never auto-close on the other tab's behalf.
//  - corrupt / missing payload → silent no-op (we already trust
//    `_loadPrepareMap`'s safe parsing on the chip side; this
//    listener is defense-in-depth).
window.addEventListener('storage', (ev) => {
  if (ev.key !== PREPARE_STORAGE_KEY) return;
  // Reuse the canonical safe-parse helper so future schema
  // changes (e.g., a version field, migration logic) flow
  // through ONE path. localStorage is already updated in our
  // tab before the `storage` event fires (per spec — the
  // origin tab's write lands, then the event is dispatched to
  // other tabs), so _loadPrepareMap() reads the same value
  // ev.newValue would have — no separate JSON.parse try/catch
  // needed here. The helper already tolerates null, parse
  // errors, non-objects, and array payloads by returning {}.
  const map = _loadPrepareMap();
  // Re-render recipe cards so the Resume chip shows the latest
  // persisted step (the chip reads localStorage at render time,
  // so refreshRecipes() picks up the new value automatically).
  if (typeof refreshRecipes === 'function') {
    refreshRecipes().catch(() => { /* network-fault tolerance */ });
  }
  // Sync the dialog if our OPEN recipe is the one whose entry
  // just changed. Strict in-range validation: never accept a step
  // past the end of THIS tab's local steps list (could differ if
  // the recipe was re-imported in one tab with a different
  // instruction count).
  if (prepareState && prepareState.recipe && Array.isArray(prepareState.steps)) {
    const rid = String(prepareState.recipe.id);
    const entry = map.entries[rid];
    if (entry
        && Number.isInteger(entry.step)
        && Number.isInteger(entry.totalSteps)
        && entry.totalSteps === prepareState.steps.length
        && entry.step >= 0
        && entry.step < entry.totalSteps) {
      prepareState.step = entry.step;
      if (typeof renderPrepareStep === 'function') {
        try { renderPrepareStep(); } catch (_) { /* resilience */ }
      }
    }
  }
});


// ----------------------------------------------------------------------------
// CONDITIONAL COMMONJS EXPORT — Node.js testability.
// ----------------------------------------------------------------------------
// The browser has no `module` global, so the `typeof` guard is `false`
// and this block is skipped at runtime in production. In Node (test
// runner, smoke tests), it exposes the helpers as a requireable object
// so the API contract can be verified without spinning up a browser.
// See tests/test_prepare_storage.js.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    PREPARE_STORAGE_KEY,
    PREPARE_SCHEMA_VERSION,
    _wrapPrepareMap,
    _loadPrepareMap,
    _savePrepareMap,
    _loadPrepareProgress,
    _savePrepareProgress,
    _clearPrepareProgress,
  };
}
