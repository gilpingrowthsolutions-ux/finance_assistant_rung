// !!! WARNING — transactions.js is loaded BEFORE the inline body script.
// !! The exported functions reference globals defined in the inline
// !! body script (`api`, `escapeHtml`, `fmt`, `flash`, `refreshOverview`).
// !! Those references are resolved at CALL-time (when the inline body's
// !! init() runs), not at LOAD-time. Don't move these references into
// !! module-level code without also moving the <script> tag to AFTER
// !! the inline body script.

/**
 * Transactions + Recurring-Bills UI module
 * ========================================
 *
 * Standalone module extracted from templates/index.html so the
 * logged-expense + recurring-bill flows can be tested in isolation.
 * Loaded via <script src="/static/js/transactions.js"></script>
 * in <head> AFTER grocery.js.
 *
 * Public API (callable as globals from the main inline script):
 *   - refreshTransactions              Renders the logged-expense list.
 *   - refreshBills                     Renders the recurring-bills list.
 *   - setupTransactionsInit(deps)      Wires the Log Expense form.
 *   - setupBillsInit(deps)             Wires the Add Bill form.
 *
 * Dependencies (resolved at call-time, not load-time):
 *   - `api`               from the main inline script.
 *   - `escapeHtml`        from the main inline script.
 *   - `fmt`               from the main inline script (currency formatter).
 *   - `flash`             from the main inline script (toast).
 *   - `refreshOverview`   from the main inline script (Overview-tab refresh).
 *   - `document`          from the global browser object.
 *
 * Node.js testability: the conditional `module.exports` block at the
 * bottom exports the helpers for isolation testing. The browser ignores
 * that block (no `module` global in browser globals).
 */

'use strict';

// ----------------------------------------------------------------------------
// PLACEHOLDER GLOBALS (resolved at call-time by the inline body script).
// ----------------------------------------------------------------------------
// Same rationale as in recipes.js / grocery.js: declare empty lets at top
// so the TDZ window is empty from the moment the script begins executing.
// The inline body script must NOT redeclare any of these.
var api_;
var escapeHtml;
var fmtGlobal_;
var flash_;
var refreshOverview_;

/**
 * Internal escapeHtml wrapper. Falls back to the inline-script
 * `escapeHtml` global, or to a tiny inline implementation if absent.
 */
function escapeHtml_(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(s);
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Internal currency formatter. Falls back to inline-script `fmt`,
 * or to a tiny inline implementation if absent.
 */
function fmt_(n) {
  if (typeof fmtGlobal_ === 'function') return fmtGlobal_(n);
  return '$' + (Number(n) || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Three-tier fetch wrapper (mirrors recipes.js + grocery.js):
 *   1. `api_` global (set by the inline body script's `api()` function).
 *   2. `_txMockFetch` test hook (set by tests via SUT._setMockFetch).
 *   3. Direct `globalThis.fetch` with defensive throw if missing.
 */
async function fetchTx_(method, path, body) {
  if (typeof api_ === 'function') return api_(method, path, body);
  if (typeof _txMockFetch === 'function') return _txMockFetch(method, path, body);
  if (typeof globalThis !== 'undefined' && typeof globalThis.fetch === 'function') {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await globalThis.fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  }
  throw new Error('transactions.js: no api function or fetch available');
}

/**
 * Fetch /api/transactions and render the logged-expense list.
 * Wires per-row Delete buttons (DELETE /transactions/<id>, legacy).
 */
async function refreshTransactions() {
  const list = document.getElementById('transactionList');
  const resp = await fetchTx_('GET', '/api/transactions', undefined);
  list.innerHTML = '';
  if (!resp.ok) {
    list.innerHTML = '<div class="empty">We could not load your transactions right now.</div>';
    return;
  }
  const data = resp.data || [];
  if (!Array.isArray(data) || data.length === 0) {
    list.innerHTML = '<div class="empty">No transactions yet.</div>';
    return;
  }
  data.forEach(t => {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `
      <div class="li-title">${escapeHtml_(t.description || '')}</div>
      <div class="li-amount">${fmt_(t.amount)}</div>
      <div class="li-meta">${escapeHtml_(t.category || '')}</div>
      <div class="li-meta">${escapeHtml_(t.date || '')}</div>
      <button class="btn is-danger" type="button" data-action="del" data-id="${t.id}">Remove</button>
    `;
    list.appendChild(row);
  });
  list.querySelectorAll('button[data-action="del"]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetchTx_('DELETE', '/transactions/' + b.dataset.id, undefined);
      if (typeof flash_ === 'function') flash_('Transaction deleted', 'success');
      await refreshTransactions();
      if (typeof refreshOverview_ === 'function') await refreshOverview_();
    });
  });
}

/**
 * Wire the Log New Expense form (POST /api/transactions). Call this
 * once from the inline body's init() AFTER the DOM is ready.
 *
 * @param {object} deps
 * @param {Function} deps.flash             toast notification (msg, kind)
 * @param {Function} deps.refreshTransactions  logged-expense refresh
 */
function setupTransactionsInit(deps) {
  deps = deps || {};  // Bind module-scoped placeholders read by click handlers above
  // and by the trailing refreshOverview call below.
  // Inline-script globals fall through when deps don't provide them;
  // typeof guards keep the read safe in Node tests where these are
  // undeclared (would otherwise throw ReferenceError).
  flash_ = (deps && typeof deps.flash === 'function') ? deps.flash
         : (typeof flash === 'function' ? flash : null);
  refreshOverview_ = (deps && typeof deps.refreshOverview === 'function')
                   ? deps.refreshOverview
                   : (typeof refreshOverview === 'function' ? refreshOverview : null);
  const refreshFn = deps.refreshTransactions || refreshTransactions;
  const flashFn = deps.flash || flash;

  const form = document.getElementById('logExpenseForm');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const desc = document.getElementById('tDesc');
    const amt = document.getElementById('tAmt');
    const cat = document.getElementById('tCat');
    const body = {
      description: (desc && desc.value ? desc.value.trim() : ''),
      amount: parseFloat(amt && amt.value ? amt.value : '0'),
      category: cat && cat.value ? cat.value : '',
    };
    if (!body.description) { if (flashFn) flashFn('Add a short description.', 'error'); return; }
    const resp = await fetchTx_('POST', '/api/transactions', body);
    if (!resp.ok) {
      if (flashFn) flashFn('We could not add this expense right now.', 'error');
      return;
    }
    if (desc) desc.value = '';
    if (flashFn) flashFn('Expense added', 'success');
    await refreshFn();
    if (typeof refreshOverview_ === 'function') await refreshOverview_();
  });
}

/**
 * Fetch /bills and render the recurring-bills list. Wires per-row
 * Toggle-paid (POST /bills/<id>/pay) and Delete (DELETE /bills/<id>).
 */
async function refreshBills() {
  const list = document.getElementById('billsList');
  const resp = await fetchTx_('GET', '/bills', undefined);
  list.innerHTML = '';
  if (!resp.ok) {
    list.innerHTML = '<div class="empty">We could not load your bills right now.</div>';
    return;
  }
  const data = resp.data || [];
  if (!Array.isArray(data) || data.length === 0) {
    list.innerHTML = '<div class="empty">No recurring bills recorded yet.</div>';
    return;
  }
  data.forEach(b => {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `
      <div class="li-title">${escapeHtml_(b.name)}</div>
      <div class="li-amount">${fmt_(b.amount)}</div>
      <div class="li-meta">Due: ${escapeHtml_(b.due_date || '')}</div>
      <div><span class="badge ${b.is_paid ? 'is-paid' : ''}">${b.is_paid ? 'Paid' : 'Unpaid'}</span></div>
      <div class="row" style="gap:6px;">
        <button class="btn is-ghost" type="button" data-action="toggle" data-id="${b.id}">${b.is_paid ? 'Mark Unpaid' : 'Mark Paid'}</button>
        <button class="btn is-danger" type="button" data-action="del" data-id="${b.id}">Remove</button>
      </div>
    `;
    list.appendChild(row);
  });
  list.querySelectorAll('button[data-action="toggle"]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetchTx_('POST', '/bills/' + b.dataset.id + '/pay', undefined);
      if (typeof flash_ === 'function') flash_('Bill status updated', 'success');
      await refreshBills();
      if (typeof refreshOverview_ === 'function') await refreshOverview_();
    });
  });
  list.querySelectorAll('button[data-action="del"]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetchTx_('DELETE', '/bills/' + b.dataset.id, undefined);
      if (typeof flash_ === 'function') flash_('Bill deleted', 'success');
      await refreshBills();
      if (typeof refreshOverview_ === 'function') await refreshOverview_();
    });
  });
}

/**
 * Wire the Add Recurring Bill form (POST /bills). Call this once
 * from the inline body's init() AFTER the DOM is ready.
 *
 * @param {object} deps
 * @param {Function} deps.flash          toast notification (msg, kind)
 * @param {Function} deps.refreshBills   recurring-bills refresh
 */
function setupBillsInit(deps) {
  deps = deps || {};  // Bind module-scoped placeholders read by click handlers above
  // and by the trailing refreshOverview call below.
  // Inline-script globals fall through when deps don't provide them;
  // typeof guards keep the read safe in Node tests where these are
  // undeclared (would otherwise throw ReferenceError).
  flash_ = (deps && typeof deps.flash === 'function') ? deps.flash
         : (typeof flash === 'function' ? flash : null);
  refreshOverview_ = (deps && typeof deps.refreshOverview === 'function')
                   ? deps.refreshOverview
                   : (typeof refreshOverview === 'function' ? refreshOverview : null);
  const refreshFn = deps.refreshBills || refreshBills;
  const flashFn = deps.flash || flash;

  const form = document.getElementById('addBillForm');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = document.getElementById('bName');
    const amt = document.getElementById('bAmt');
    const date = document.getElementById('bDate');
    const body = {
      name: name && name.value ? name.value.trim() : '',
      amount: parseFloat(amt && amt.value ? amt.value : '0'),
      due_date: date && date.value ? date.value : '',
    };
    if (!body.name) { if (flashFn) flashFn('Enter a bill name.', 'error'); return; }
    const resp = await fetchTx_('POST', '/bills', body);
    if (!resp.ok) {
      if (flashFn) flashFn('We could not add this bill right now.', 'error');
      return;
    }
    // Reset each input individually (mirrors setupTransactionsInit's pattern).
    // form.reset() relies on child elements being reachable via form.children,
    // which is fragile when the page looks them up separately by id — explicit
    // per-field clears work the same in the browser and keep intent local.
    if (name) name.value = '';
    if (amt) amt.value = '';
    if (date) date.value = '';
    if (flashFn) flashFn('Bill added', 'success');
    await refreshFn();
    if (typeof refreshOverview_ === 'function') await refreshOverview_();
  });
}

// ----------------------------------------------------------------------------
// TEST HOOKS (Node only — browser never touches these).
// ----------------------------------------------------------------------------
let _txMockFetch;

// ----------------------------------------------------------------------------
// CONDITIONAL COMMONJS EXPORT — Node.js testability.
// ----------------------------------------------------------------------------
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    refreshTransactions,
    refreshBills,
    setupTransactionsInit,
    setupBillsInit,
    _setMockFetch:   (fn) => { _txMockFetch = fn; },
    _resetMockFetch: () => { _txMockFetch = null; },
  };
}
