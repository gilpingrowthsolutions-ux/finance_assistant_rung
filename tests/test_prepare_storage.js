/**
 * Node.js smoke test for static/js/prepare-storage.js.
 *
 * Exercises the public API contract end-to-end in isolation:
 *   - Round-trip _savePrepareProgress -> _loadPrepareProgress.
 *   - _clearPrepareProgress removal.
 *   - Migration from legacy v0 shape to v1 wrapped shape on next save.
 *   - Malformed payload fallback to empty v1.
 *   - recipeId string-coercion invariant (integer keys behave like strings).
 *   - Defensive null recipeId handling.
 *
 * Run with:  node tests/test_prepare_storage.js
 */

'use strict';

// ---- Browser-globals mocks ----
const mockStorage = new Map();
global.localStorage = {
  getItem: (k) => mockStorage.has(k) ? mockStorage.get(k) : null,
  setItem: (k, v) => mockStorage.set(k, String(v)),
  removeItem: (k) => mockStorage.delete(k),
  clear: () => mockStorage.clear(),
};
const storageListeners = [];
global.window = {
  addEventListener: (event, fn) => {
    if (event === 'storage') storageListeners.push(fn);
  },
};
// Date / Math are provided by Node natively.

// ---- System under test ----
const SUT = require('../static/js/prepare-storage.js');

let passed = 0;
let failed = 0;

function assertEq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    passed++;
    console.log(`  ✓ ${label}`);
  } else {
    failed++;
    console.error(`  ✗ ${label}: expected ${e}, got ${a}`);
  }
}

function reset() {
  mockStorage.clear();
}

// ============================================================================
console.log('1. Empty-state behavior');
// ============================================================================
reset();
assertEq(SUT._loadPrepareProgress(1), null, 'returns null when key absent');
assertEq(SUT._loadPrepareProgress('1'), null, 'returns null for string key when absent');
assertEq(SUT._loadPrepareProgress(null), null, 'returns null for null recipeId');
assertEq(SUT._loadPrepareProgress(undefined), null, 'returns null for undefined recipeId');

// ============================================================================
console.log('\n2. Round-trip _savePrepareProgress -> _loadPrepareProgress');
// ============================================================================
reset();
SUT._savePrepareProgress('recipe-7', 2, 5);
const loaded = SUT._loadPrepareProgress('recipe-7');
assertEq(loaded && loaded.step, 2, 'round-trip preserves step');
assertEq(loaded && loaded.totalSteps, 5, 'round-trip preserves totalSteps');
assertEq(typeof loaded.savedAt, 'string', 'round-trip adds ISO savedAt string');
// savedAt must round-trip through Date (validates ISO-8601 format).
assertEq(new Date(loaded.savedAt).toISOString(), loaded.savedAt, 'savedAt is a valid ISO-8601 string');
assertEq(mockStorage.has(SUT.PREPARE_STORAGE_KEY), true, 'persisted to localStorage');

const onDisk = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(onDisk.v, SUT.PREPARE_SCHEMA_VERSION, 'on-disk payload has v = PREPARE_SCHEMA_VERSION');
assertEq(typeof onDisk.entries, 'object', 'on-disk payload has entries object');

// ============================================================================
console.log('\n3. Integer recipeId coercion (passes through to string lookup)');
// ============================================================================
reset();
SUT._savePrepareProgress(42, 1, 4);
assertEq(SUT._loadPrepareProgress(42)    && SUT._loadPrepareProgress(42).step, 1, 'integer key round-trip');
assertEq(SUT._loadPrepareProgress('42')  && SUT._loadPrepareProgress('42').step, 1, 'string-form of same integer key matches');

// ============================================================================
console.log('\n4. _clearPrepareProgress');
// ============================================================================
reset();
SUT._savePrepareProgress('a', 0, 3);
SUT._savePrepareProgress('b', 1, 3);
SUT._clearPrepareProgress('a');
assertEq(SUT._loadPrepareProgress('a'), null, 'cleared entry returns null');
assertEq(SUT._loadPrepareProgress('b') && SUT._loadPrepareProgress('b').step, 1, 'other entries preserved');
SUT._clearPrepareProgress('nonexistent'); // no-op
assertEq(SUT._loadPrepareProgress('b') && SUT._loadPrepareProgress('b').step, 1, 'no-op clear preserves other entries');

// ============================================================================
console.log('\n5. Defensive null recipeId');
// ============================================================================
reset();
SUT._savePrepareProgress(null, 1, 1);    // silent no-op
SUT._savePrepareProgress(undefined, 1, 1); // silent no-op
SUT._clearPrepareProgress(null);          // silent no-op
assertEq(mockStorage.has(SUT.PREPARE_STORAGE_KEY), false, 'null recipeId writes never touched localStorage');

// ============================================================================
console.log('\n6. Step / totalSteps clamping');
// ============================================================================
reset();
SUT._savePrepareProgress('r', -5, 0);
const clamped = SUT._loadPrepareProgress('r');
assertEq(clamped.step, 0, 'step clamped to >= 0');
assertEq(clamped.totalSteps, 1, 'totalSteps clamped to >= 1');

// ============================================================================
console.log('\n7. Legacy v0 migration on read');
// ============================================================================
reset();
// Pre-versioning shape: bare inner map with no top-level `v` field.
mockStorage.set(SUT.PREPARE_STORAGE_KEY, JSON.stringify({
  'legacy-recipe': { step: 1, totalSteps: 3, savedAt: '2025-01-01T00:00:00.000Z' },
}));
const legacyLoaded = SUT._loadPrepareProgress('legacy-recipe');
assertEq(legacyLoaded && legacyLoaded.step, 1, 'legacy v0 entry readable via _loadPrepareProgress');
// Save should normalize to v1 on next write.
SUT._savePrepareProgress('legacy-recipe', 2, 3);
const afterWrite = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(afterWrite.v, 1, 'write-through migration: payload now has v=1');
assertEq(afterWrite.entries && afterWrite.entries['legacy-recipe'] && afterWrite.entries['legacy-recipe'].step, 2, 'migrated entry updated to step 2');

// ============================================================================
console.log('\n8. Malformed payload -> empty v1 fallback (no crash)');
// ============================================================================
reset();
mockStorage.set(SUT.PREPARE_STORAGE_KEY, 'not-valid-json{');
assertEq(SUT._loadPrepareProgress('anything'), null, 'corrupt JSON returns null on lookup');
SUT._savePrepareProgress('after-corrupt', 1, 1); // should succeed + write clean v1
const recovered = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(recovered.v, 1, 'save after corrupt payload writes clean v1');
assertEq(recovered.entries && recovered.entries['after-corrupt'] && recovered.entries['after-corrupt'].step, 1, 'save after corrupt payload persists new entry');

// ============================================================================
console.log('\n9. Multi-recipe isolation');
// ============================================================================
reset();
SUT._savePrepareProgress('recipe-A', 1, 4);
SUT._savePrepareProgress('recipe-B', 2, 5);
SUT._savePrepareProgress('recipe-C', 3, 6);
const aBefore = SUT._loadPrepareProgress('recipe-A');
SUT._savePrepareProgress('recipe-B', 4, 5); // mutate B, expect A + C untouched
const aAfter  = SUT._loadPrepareProgress('recipe-A');
const cAfter  = SUT._loadPrepareProgress('recipe-C');
const bAfter  = SUT._loadPrepareProgress('recipe-B');
assertEq(aBefore.step, 1, 'recipe-A pre-mutation step = 1');
assertEq(aAfter.step, 1, 'recipe-A step untouched after mutating B');
assertEq(aAfter.totalSteps, 4, 'recipe-A totalSteps untouched after mutating B');
assertEq(cAfter.step, 3, 'recipe-C step untouched after mutating B');
assertEq(cAfter.totalSteps, 6, 'recipe-C totalSteps untouched after mutating B');
assertEq(bAfter.step, 4, 'recipe-B step updated to 4');
assertEq(bAfter.totalSteps, 5, 'recipe-B totalSteps preserved');

// ============================================================================
console.log('\n10. _wrapPrepareMap direct invocation (private normalizer)');
// ============================================================================
reset();
// Strict-v1 input is returned as-is (no defensive copy).
const strictV1 = { v: 1, entries: { 'a': { step: 0, totalSteps: 1, savedAt: 'x' } } };
const wrappedStrict = SUT._wrapPrepareMap(strictV1);
assertEq(wrappedStrict, strictV1, 'strict v1 input returns as-is (identity)');

// Legacy bare inner map is wrapped with v header.
const legacy = { 'a': { step: 0, totalSteps: 1, savedAt: 'x' } };
const wrappedLegacy = SUT._wrapPrepareMap(legacy);
assertEq(wrappedLegacy.v, 1, 'legacy inner map gets v=1');
assertEq(wrappedLegacy.entries, legacy, 'legacy entries are preserved verbatim');

// Defensive fallbacks return empty v1.
assertEq(SUT._wrapPrepareMap(null).entries, {}, 'null fallback returns empty v1 entries');
assertEq(SUT._wrapPrepareMap(undefined).entries, {}, 'undefined fallback returns empty v1 entries');
assertEq(SUT._wrapPrepareMap([]).entries, {}, 'array fallback returns empty v1 entries');
assertEq(SUT._wrapPrepareMap({}).entries, {}, 'empty object fallback returns empty v1 entries');

// ============================================================================
console.log('\n11. _savePrepareMap direct invocation (write-through)');
// ============================================================================
reset();
SUT._savePrepareMap({});
const afterEmptySave = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(afterEmptySave.v, 1, 'empty save writes v1 wrapper');
assertEq(Object.keys(afterEmptySave.entries).length, 0, 'empty save writes empty entries');

// Save a wrapped map directly.
SUT._savePrepareMap({ v: 1, entries: { 'x': { step: 1, totalSteps: 2, savedAt: 'iso' } } });
const afterDirectSave = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(afterDirectSave.entries.x.step, 1, 'direct save preserves entry');
assertEq(afterDirectSave.entries.x.totalSteps, 2, 'direct save preserves totalSteps');

// Save a legacy inner map directly — the next read should migrate it transparently.
SUT._savePrepareMap({ 'legacy': { step: 0, totalSteps: 1, savedAt: 'iso' } });
const afterLegacySave = JSON.parse(mockStorage.get(SUT.PREPARE_STORAGE_KEY));
assertEq(afterLegacySave.v, 1, 'legacy save writes v1 wrapper (write-through migration)');

// ============================================================================
// Final report. All current tests are synchronous; if a future scenario
// needs async coordination (e.g., awaiting a fetch or a storage event),
// wrap the bodies in `async function main() { ... } main().then(...)`.
// ============================================================================
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
