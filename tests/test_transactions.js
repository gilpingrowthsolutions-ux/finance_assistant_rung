/**
 * Node.js smoke test for static/js/transactions.js.
 *
 * Mirrors test_recipes.js / test_grocery.js structure:
 *   - mockRoutes / mockFetch for API mocking (with optional body-match).
 *   - FakeEl DOM registry with addEventListener + appendChild + querySelectorAll.
 *   - 12 scenarios covering refreshTransactions + logExpenseForm +
 *     refreshBills + addBillForm happy paths, error paths, and edge cases.
 *
 * Run with:  node tests/test_transactions.js
 */

'use strict';

const fs = require('fs');
const path = require('path');

// ---- Mock fetch infrastructure ----
const mockRoutes = [];
function mockRoute(method, path, status, responseBody, expectedBody) {
  mockRoutes.push({ method, path, status, responseBody, expectedBody });
}
function clearMockRoutes() { mockRoutes.length = 0; }
async function mockFetch(method, path, body) {
  const route = mockRoutes.find(r => {
    if (r.method !== method || r.path !== path) return false;
    if (r.expectedBody === undefined) return true;
    return JSON.stringify(r.expectedBody) === JSON.stringify(body);
  });
  if (!route) throw new Error('No mock for ' + method + ' ' + path + (body ? ' ' + JSON.stringify(body) : ''));
  return { ok: route.status >= 200 && route.status < 300, status: route.status, data: route.responseBody };
}

// ---- Mock DOM registry ----
class FakeEl {
  constructor(id) {
    this.id = id;
    this.value = '';
    this.disabled = false;
    this.textContent = '';
    this.style = {};
    // _innerHTML is the raw markup; setting innerHTML parses any <button>
    // tags into synthetic children so querySelectorAll can find them.
    this._innerHTML = '';
    this.children = [];
    this.eventListeners = {};
    this.dataset = {};
    this.attributes = {};
    this.tagName = 'div';
  }
  // ---- innerHTML getter/setter ----------------------------------------------
  // refreshTransactions / refreshBills build rows with
  // `row.innerHTML = '<button data-action="del" data-id="99">Delete</button>'`
  // then call `list.querySelectorAll('button[data-action="del"]').forEach(b => b.addEventListener(...))`.
  // For the test to verify handler registration + invocation, FakeEl needs to
  // turn those HTML strings into synthetic button children with the right
  // data-action / data-id attributes.
  get innerHTML() { return this._innerHTML; }
  set innerHTML(html) {
    this._innerHTML = html || '';
    this.children = []; // reset; new HTML means new children
    if (this._innerHTML && this._innerHTML.indexOf('<button') !== -1) {
      this._parseInnerHTMLButtons_(this._innerHTML);
    }
  }
  // Parse `<button ... data-action="X" data-id="N">text</button>` out of the
  // markup and create one synthetic FakeEl child per match. Restricted to the
  // patterns our production code emits (data-action + optional data-id +
  // plain text content) — good enough for the smoke test scope.
  _parseInnerHTMLButtons_(html) {
    const re = /<button\b([^>]*?)>([^<]*?)<\/button>/g;
    let m;
    while ((m = re.exec(html)) !== null) {
      const attrStr = m[1] || '';
      const text = (m[2] || '').trim();
      const actionMatch = attrStr.match(/data-action=["'](\w+)["']/);
      if (!actionMatch) continue;
      const idMatch = attrStr.match(/data-id=["']([^"']+)["']/);
      const btn = new FakeEl('btn-' + actionMatch[1] + (idMatch ? '-' + idMatch[1] : ''));
      btn.tagName = 'button';
      btn.attributes['data-action'] = actionMatch[1];
      if (idMatch) {
        btn.attributes['data-id'] = idMatch[1];
        btn.dataset.id = idMatch[1];
      }
      btn.dataset.action = actionMatch[1];
      btn.textContent = text;
      this.children.push(btn);
    }
  }
  // ----------------------------------------------------------------- end pars
  // Trigger the registered click handler synchronously (test-only path).
  // The test suite mostly invokes `el.eventListeners.click[0]()` directly,
  // but this is here for tests that want a DOM-faithful `.click()`.
  click() {
    const handlers = this.eventListeners.click || [];
    if (!handlers.length) return undefined;
    const ev = { preventDefault: () => {}, target: this };
    return handlers[0](ev);
  }
  addEventListener(event, fn) {
    (this.eventListeners[event] = this.eventListeners[event] || []).push(fn);
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getAttribute(k) { return this.attributes[k]; }
  querySelectorAll(sel) {
    // Recursively find every descendant matching the selector. Mirrors real
    // DOM behavior so `list.querySelectorAll('button[data-action=...]')`
    // finds <button> nested inside list -> row -> <button> (the production
    // pattern: row.innerHTML = '...<button>...'; list.appendChild(row)).
    const results = [];
    const collect = (el) => {
      if (this._matchesSelector_(el, sel)) results.push(el);
      for (const child of (el.children || [])) collect(child);
    };
    for (const child of this.children) collect(child);
    return results;
  }
  // Predicate: does this FakeEl match the simplified selector grammar we use?
  // Supported: tag-only, [data-action="X"], [data-id="X"], or combinations
  // like button[data-action="del"] (the tag prefix is ignored for now).
  _matchesSelector_(el, sel) {
    const mAction = sel.match(/\[data-action=["'](\w+)["']\]/);
    if (mAction) {
      return el.attributes && el.attributes['data-action'] === mAction[1];
    }
    const mDataId = sel.match(/\[data-id=["']([^"']+)["']\]/);
    if (mDataId) {
      return el.attributes && el.attributes['data-id'] === mDataId[1];
    }
    const mAnyDataId = sel.match(/^button\[data-id\]/);
    if (mAnyDataId) {
      return el.attributes && !!el.attributes['data-id'];
    }
    return false;
  }
  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length > 0 ? all[0] : null;
  }
  reset() { this.value = ''; this.attributes = {}; }
}

const fakeDom = new Map();
function setupFakeDom(idMap) {
  fakeDom.clear();
  for (const [id, value] of Object.entries(idMap)) {
    fakeDom.set(id, value !== undefined ? value : new FakeEl(id));
  }
}
global.document = {
  getElementById: (id) => fakeDom.get(id) || null,
  createElement: (tag) => new FakeEl(tag),
};

// ---- System under test ----
const SUT = require('../static/js/transactions.js');

// ---- Assertions ----
let passed = 0, failed = 0;
function assertEq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) { passed++; console.log('  \u2713 ' + label); }
  else { failed++; console.error('  \u2717 ' + label + ': expected ' + e + ', got ' + a); }
}
function reset() {
  clearMockRoutes();
  fakeDom.clear();
}

// ============================================================================
async function main() {

console.log('1. refreshTransactions happy path: GET returns array, renders rows');
reset();
const txList1 = new FakeEl('transactionList');
setupFakeDom({ 'transactionList': txList1 });
mockRoute('GET', '/api/transactions', 200, [
  { id: 1, description: 'Hardware Store', amount: 25.50, category: 'discretionary', date: '2025-01-15' },
  { id: 2, description: 'Electric Bill', amount: 75.00, category: 'essentials', date: '2025-01-14' },
]);
SUT._setMockFetch(mockFetch);
await SUT.refreshTransactions();
assertEq(txList1.children.length, 2, 'renders 2 transaction rows');
assertEq(txList1.children[0].innerHTML.includes('Hardware Store'), true, 'row 1 title correct');
assertEq(txList1.children[0].innerHTML.includes('$25.50'), true, 'row 1 amount formatted');
assertEq(txList1.children[1].innerHTML.includes('Electric Bill'), true, 'row 2 title correct');

console.log('\n2. refreshTransactions empty: GET returns [], shows empty state');
reset();
const txList2 = new FakeEl('transactionList');
setupFakeDom({ 'transactionList': txList2 });
mockRoute('GET', '/api/transactions', 200, []);
SUT._setMockFetch(mockFetch);
await SUT.refreshTransactions();
assertEq(txList2.innerHTML.includes('No transactions yet'), true, 'empty state when no transactions');
assertEq(txList2.innerHTML.includes('data-money-open="transaction"'), true, 'transaction empty state offers one useful action');

console.log('\n3. refreshTransactions error: GET fails, shows error empty state');
reset();
const txList3 = new FakeEl('transactionList');
setupFakeDom({ 'transactionList': txList3 });
mockRoute('GET', '/api/transactions', 500, { error: 'db unavailable' });
SUT._setMockFetch(mockFetch);
await SUT.refreshTransactions();
assertEq(txList3.innerHTML.includes('Transactions could not be loaded'), true, 'error empty state');
assertEq(txList3.innerHTML.includes('retry-transactions'), true, 'transaction error offers retry');

console.log('\n4. refreshTransactions Delete button: clicking triggers DELETE + refresh');
reset();
const txList4 = new FakeEl('transactionList');
setupFakeDom({ 'transactionList': txList4 });
// First GET to populate the list.
mockRoute('GET', '/api/transactions', 200, [{ id: 99, description: 'Test', amount: 10, category: 'misc', date: '2025-01-01' }]);
mockRoute('DELETE', '/transactions/99', 200, { message: 'Transaction deleted' });
const capturedFlash4 = [];
let overviewCalled4 = 0;
SUT._setMockFetch(mockFetch);
await SUT.refreshTransactions();
SUT.setupTransactionsInit({
  flash: (msg, kind) => capturedFlash4.push({ msg, kind }),
});
// Wire refreshOverview mock by setting a placeholder.
// Mirror the inline-script's window.flash definition so the bare-global click handler can resolve it.
// Click the delete button.
const delBtn = txList4.children[0].querySelectorAll('button[data-action="del"]')[0];
await delBtn.eventListeners.click[0]();
assertEq(capturedFlash4.length, 1, 'flash called once after delete');
assertEq(capturedFlash4[0].kind, 'success', 'flash kind success after delete');


console.log('\n5. logExpenseForm happy path: POSTs body, resets form, refreshes');
reset();
setupFakeDom({
  'logExpenseForm':  new FakeEl('logExpenseForm'),
  'tDesc':           (() => { const e = new FakeEl('tDesc'); e.value = 'Coffee'; return e; })(),
  'tAmt':            (() => { const e = new FakeEl('tAmt'); e.value = '4.50'; return e; })(),
  'tCat':            (() => { const e = new FakeEl('tCat'); e.value = 'discretionary'; return e; })(),
  'transactionList': new FakeEl('transactionList'),
});
let capturedExpenseBody = null;
let expenseMutationCalls5 = 0;
mockRoute('POST', '/api/transactions', 200, { id: 100, description: 'Coffee' }, { description: 'Coffee', amount: 4.5, category: 'discretionary' });
SUT._setMockFetch((m, p, b) => {
  if (p === '/api/transactions' && m === 'POST') { expenseMutationCalls5++; capturedExpenseBody = b; return mockFetch(m, p, b); }
  return mockFetch(m, p, b);
});
let refreshTxCalled5 = 0;
let refreshOverviewCalled5 = 0;
const capturedFlash5 = [];
SUT.setupTransactionsInit({
  flash: (msg, kind) => capturedFlash5.push({ msg, kind }),
  refreshTransactions: () => { refreshTxCalled5++; },
  refreshOverview: () => { refreshOverviewCalled5++; },
});
assertEq(fakeDom.get('logExpenseForm').eventListeners.submit.length, 1, 'one Transaction submit handler registered');
await fakeDom.get('logExpenseForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(expenseMutationCalls5, 1, 'one Transaction submit produces one mutation call');
assertEq(capturedExpenseBody && capturedExpenseBody.description, 'Coffee', 'POST body has correct description');
assertEq(capturedExpenseBody && capturedExpenseBody.amount, 4.5, 'POST body has correct amount');
assertEq(fakeDom.get('tDesc').value, '', 'description input cleared after post');
assertEq(capturedFlash5.length, 1, 'flash called on success');
assertEq(capturedFlash5[0].kind, 'success', 'flash kind success');
assertEq(refreshTxCalled5, 1, 'refreshTransactions called after post');
assertEq(refreshOverviewCalled5, 1, 'refreshOverview called after transaction post');

console.log('\n6. logExpenseForm empty description: rejected before fetch, no flash for success');
reset();
setupFakeDom({
  'logExpenseForm':  new FakeEl('logExpenseForm'),
  'tDesc':           (() => { const e = new FakeEl('tDesc'); e.value = '   '; return e; })(),
  'tAmt':            (() => { const e = new FakeEl('tAmt'); e.value = '5'; return e; })(),
  'tCat':            (() => { const e = new FakeEl('tCat'); e.value = 'misc'; return e; })(),
});
let fetchCalled6 = false;
const capturedFlash6 = [];
SUT.setupTransactionsInit({
  flash: (msg, kind) => capturedFlash6.push({ msg, kind }),
  refreshTransactions: () => { fetchCalled6 = true; },
});
SUT._setMockFetch(() => { fetchCalled6 = true; return mockFetch.apply(null, arguments); });
await fakeDom.get('logExpenseForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fetchCalled6, false, 'fetch NOT called on empty description');
assertEq(capturedFlash6.length, 1, 'flash called once for empty description');
assertEq(capturedFlash6[0].kind, 'error', 'flash kind error for empty description');

console.log('\n7. logExpenseForm server error: flash with API error, no refresh');
reset();
setupFakeDom({
  'logExpenseForm': new FakeEl('logExpenseForm'),
  'tDesc':          (() => { const e = new FakeEl('tDesc'); e.value = 'X'; return e; })(),
  'tAmt':           (() => { const e = new FakeEl('tAmt'); e.value = '10'; return e; })(),
  'tCat':           (() => { const e = new FakeEl('tCat'); e.value = 'misc'; return e; })(),
});
mockRoute('POST', '/api/transactions', 400, { error: 'invalid amount' });
const capturedFlash7 = [];
let refreshTxCalled7 = 0;
SUT.setupTransactionsInit({
  flash: (msg, kind) => capturedFlash7.push({ msg, kind }),
  refreshTransactions: () => { refreshTxCalled7++; },
});
SUT._setMockFetch(mockFetch);
await fakeDom.get('logExpenseForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(capturedFlash7.length, 1, 'flash called on server error');
assertEq(capturedFlash7[0].kind, 'error', 'flash kind error');
assertEq(capturedFlash7[0].msg, 'We could not add this expense right now.', 'flash shows user-safe error message');
assertEq(refreshTxCalled7, 0, 'refreshTransactions NOT called on error');

console.log('\n8. refreshBills happy path: GET returns array, renders rows with badges');
reset();
const billsList8 = new FakeEl('billsList');
setupFakeDom({ 'billsList': billsList8 });
mockRoute('GET', '/bills', 200, [
  { id: 5, name: 'Electric', amount: 75.00, due_date: '2025-02-01', is_paid: false },
  { id: 6, name: 'Internet', amount: 60.00, due_date: '2025-02-05', is_paid: true },
]);
SUT._setMockFetch(mockFetch);
await SUT.refreshBills();
assertEq(billsList8.children.length, 2, 'renders 2 bills');
assertEq(billsList8.children[0].innerHTML.includes('Electric'), true, 'row 1 name correct');
assertEq(billsList8.children[0].innerHTML.includes('Mark Paid'), true, 'row 1 shows Mark Paid (unpaid)');
assertEq(billsList8.children[1].innerHTML.includes('Internet'), true, 'row 2 name correct');
assertEq(billsList8.children[1].innerHTML.includes('Mark Unpaid'), true, 'row 2 shows Mark Unpaid (paid)');

console.log('\n9. refreshBills toggle-paid button: triggers POST /bills/<id>/pay');
reset();
const billsList9 = new FakeEl('billsList');
setupFakeDom({ 'billsList': billsList9 });
mockRoute('GET', '/bills', 200, [{ id: 7, name: 'Water', amount: 30, due_date: '2025-03-01', is_paid: false }]);
mockRoute('POST', '/bills/7/pay', 200, { message: 'Bill status updated' });
SUT._setMockFetch(mockFetch);
await SUT.refreshBills();
const capturedFlash9 = [];
SUT.setupBillsInit({
  flash: (msg, kind) => capturedFlash9.push({ msg, kind }),
});
const toggleBtn = billsList9.children[0].querySelectorAll('button[data-action="toggle"]')[0];
await toggleBtn.eventListeners.click[0]();
assertEq(capturedFlash9.length, 1, 'flash called on toggle');
assertEq(capturedFlash9[0].kind, 'success', 'flash kind success');


console.log('\n10. refreshBills Delete button: triggers DELETE /bills/<id>');
reset();
const billsList10 = new FakeEl('billsList');
setupFakeDom({ 'billsList': billsList10 });
mockRoute('GET', '/bills', 200, [{ id: 8, name: 'Garbage', amount: 20, due_date: '2025-03-15', is_paid: false }]);
mockRoute('DELETE', '/bills/8', 200, { message: 'Bill deleted' });
SUT._setMockFetch(mockFetch);
await SUT.refreshBills();
const capturedFlash10 = [];
SUT.setupBillsInit({
  flash: (msg, kind) => capturedFlash10.push({ msg, kind }),
});
const delBtn10 = billsList10.children[0].querySelectorAll('button[data-action="del"]')[0];
await delBtn10.eventListeners.click[0]();
assertEq(capturedFlash10.length, 1, 'flash called on bill delete');
assertEq(capturedFlash10[0].kind, 'success', 'flash kind success');


console.log('\n11. addBillForm happy path: POSTs body, resets form, refreshes');
reset();
setupFakeDom({
  'addBillForm': new FakeEl('addBillForm'),
  'bName':       (() => { const e = new FakeEl('bName'); e.value = 'Rent'; return e; })(),
  'bAmt':        (() => { const e = new FakeEl('bAmt'); e.value = '1200'; return e; })(),
  'bDate':       (() => { const e = new FakeEl('bDate'); e.value = '2025-04-01'; return e; })(),
});
let capturedBillBody = null;
let billMutationCalls11 = 0;
mockRoute('POST', '/bills', 200, { id: 50, name: 'Rent' }, { name: 'Rent', amount: 1200, due_date: '2025-04-01' });
SUT._setMockFetch((m, p, b) => {
  if (p === '/bills' && m === 'POST') { billMutationCalls11++; capturedBillBody = b; return mockFetch(m, p, b); }
  return mockFetch(m, p, b);
});
let refreshBillsCalled11 = 0;
let refreshOverviewCalled11 = 0;
const capturedFlash11 = [];
SUT.setupBillsInit({
  flash: (msg, kind) => capturedFlash11.push({ msg, kind }),
  refreshBills: () => { refreshBillsCalled11++; },
  refreshOverview: () => { refreshOverviewCalled11++; },
});
assertEq(fakeDom.get('addBillForm').eventListeners.submit.length, 1, 'one Bill submit handler registered');
await fakeDom.get('addBillForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(billMutationCalls11, 1, 'one Bill submit produces one mutation call');
assertEq(capturedBillBody && capturedBillBody.name, 'Rent', 'POST body has correct name');
assertEq(capturedBillBody && capturedBillBody.amount, 1200, 'POST body has correct amount');
assertEq(fakeDom.get('bName').value, '', 'bName input cleared after add');
assertEq(capturedFlash11.length, 1, 'flash called on bill add success');
assertEq(refreshBillsCalled11, 1, 'refreshBills called after add');
assertEq(refreshOverviewCalled11, 1, 'refreshOverview called after bill post');

console.log('\n12. addBillForm empty name: rejected before fetch, error flash');

console.log('\n13. addBillForm server error: flash with API error, no refresh');
reset();
setupFakeDom({
  'addBillForm': new FakeEl('addBillForm'),
  'bName':       (() => { const e = new FakeEl('bName'); e.value = 'Insurance'; return e; })(),
  'bAmt':        (() => { const e = new FakeEl('bAmt'); e.value = '200'; return e; })(),
  'bDate':       (() => { const e = new FakeEl('bDate'); e.value = '2025-05-01'; return e; })(),
});
mockRoute('POST', '/bills', 400, { error: 'amount must be positive' });
const capturedFlash13 = [];
let refreshBillsCalled13 = 0;
SUT.setupBillsInit({
  flash: (msg, kind) => capturedFlash13.push({ msg, kind }),
  refreshBills: () => { refreshBillsCalled13++; },
});
SUT._setMockFetch(mockFetch);
await fakeDom.get('addBillForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(capturedFlash13.length, 1, 'flash called on addBill server error');
assertEq(capturedFlash13[0].kind, 'error', 'flash kind error on addBill server error');
assertEq(capturedFlash13[0].msg, 'We could not add this bill right now.', 'flash shows user-safe error');
assertEq(refreshBillsCalled13, 0, 'refreshBills NOT called on error');

console.log('\n14. refreshTransactions Delete with refreshOverview undefined: handler does not crash');
reset();
const txList14 = new FakeEl('transactionList');
setupFakeDom({ 'transactionList': txList14 });
mockRoute('GET', '/api/transactions', 200, [{ id: 50, description: 'X', amount: 5, category: 'misc', date: '2025-01-01' }]);
mockRoute('DELETE', '/transactions/50', 200, { message: 'Transaction deleted' });
const capturedFlash14 = [];
SUT._setMockFetch(mockFetch);
await SUT.refreshTransactions();
// Scenario 14 contract (refreshOverview binding → null → typeof-guard skip → no throw):
//         Don't pass refreshOverview via setupXxxInit deps — refreshOverview_ falls through to null,
//         click handler's typeof guard skips it, and we assert no exception is thrown below.
SUT.setupTransactionsInit({
  flash: (msg, kind) => capturedFlash14.push({ msg, kind }),
});
// Critically: do NOT set global.refreshOverview. The handler must NOT
// throw when refreshOverview is undefined.
let threw = false;
try {
  const delBtn14 = txList14.children[0].querySelectorAll('button[data-action="del"]')[0];
await delBtn14.eventListeners.click[0]();
} catch (_) { threw = true; }
assertEq(threw, false, 'Delete handler does NOT throw when refreshOverview is undefined');
assertEq(capturedFlash14.length, 1, 'flash called once even when refreshOverview is undefined');
assertEq(capturedFlash14[0].kind, 'success', 'flash kind success after delete');
reset();
setupFakeDom({
  'addBillForm': new FakeEl('addBillForm'),
  'bName':       (() => { const e = new FakeEl('bName'); e.value = '   '; return e; })(),
  'bAmt':        (() => { const e = new FakeEl('bAmt'); e.value = '50'; return e; })(),
  'bDate':       (() => { const e = new FakeEl('bDate'); e.value = '2025-04-01'; return e; })(),
});
let fetchCalled12 = false;
const capturedFlash12 = [];
SUT.setupBillsInit({
  flash: (msg, kind) => capturedFlash12.push({ msg, kind }),
  refreshBills: () => { fetchCalled12 = true; },
});
SUT._setMockFetch(() => { fetchCalled12 = true; return mockFetch.apply(null, arguments); });
await fakeDom.get('addBillForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fetchCalled12, false, 'fetch NOT called on empty bill name');
assertEq(capturedFlash12.length, 1, 'flash called for empty bill name');
assertEq(capturedFlash12[0].kind, 'error', 'flash kind error for empty bill name');

console.log('\n15. served template delegates Transaction and Bill submits only to transactions.js');
const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
assertEq(template.includes("setupTransactionsInit({"), true, 'production initializes Transaction module handler');
assertEq(template.includes("setupBillsInit({"), true, 'production initializes Bill module handler');
assertEq(template.includes("logForm.addEventListener('submit'"), false, 'production has no duplicate inline Transaction submit handler');
assertEq(template.includes("billForm.addEventListener('submit'"), false, 'production has no duplicate inline Bill submit handler');

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);

} // end main

main().catch((err) => {
  console.error('Test runner crashed:', err);
  process.exit(2);
});
