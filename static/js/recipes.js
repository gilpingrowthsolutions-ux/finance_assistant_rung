// !!! WARNING — recipes.js depends on prepare-storage.js loading FIRST.
// !! `refreshRecipes` reads `_loadPrepareProgress` from prepare-storage.js
// !! to render the Resume chip. If you reorder these two <script> tags
// !! you'll get a ReferenceError on first recipes-list render. Today the
// !! tags are loaded synchronously in <head> with prepare-storage.js
// !! first, so the order is guaranteed.
//
// !!! WARNING — recipes.js is loaded BEFORE the inline body script.
// !! The exported functions reference globals defined in the inline
// !! body script (`escapeHtml`, `openPrepareMode`, `updateRecipeSelection`,
// !! `document`). Those references are resolved at CALL-time (when the
// !! inline body's init() runs), not at LOAD-time. Don't move these
// !! references into module-level code without also moving the <script>
// !! tag to AFTER the inline body script.

/**
 * Recipes UI module
 * =================
 *
 * Standalone module extracted from templates/index.html so the
 * recipes-import + recipe-list-refresh flow can be tested in
 * isolation. Loaded via <script src="/static/js/recipes.js"></script>
 * in <head> AFTER prepare-storage.js.
 *
 * Public API (callable as globals from the main inline script):
 *   - refreshRecipes                Renders the recipes list.
 *   - setupRecipesInit(deps)        Wires the Add + Import forms.
 *
 * Dependencies (resolved at call-time, not load-time):
 *   - `_loadPrepareProgress`  from prepare-storage.js (Resume chip).
 *   - `escapeHtml`            from the main inline script.
 *   - `openPrepareMode`       from the main inline script.
 *   - `updateRecipeSelection` from the main inline script.
 *   - `document`              from the global browser object.
 *
 * Node.js testability: the conditional `module.exports` block at the
 * bottom exports the helpers for isolation testing. The browser ignores
 * that block (no `module` global in browser globals).
 */

'use strict';

// ----------------------------------------------------------------------------
// PLACEHOLDER GLOBALS (resolved at call-time by the inline body script).
// ----------------------------------------------------------------------------
// These declarations exist SOLELY to suppress "Identifier already declared"
// errors if the inline body script also defines `api` / `escapeHtml` global
// identifiers (function-declaration hoisting across <script> tags already
// covers that case), AND so that the TDZ window inside this file is empty
// from the moment the script begins executing.
//
// IMPORTANT CONTRACT: the inline body script MUST NOT redeclare any of
// these as `let api_ = ...` / `const escapeHtml = ...` at its own top
// level — that would throw `SyntaxError: Identifier 'api_' has already
// been declared` once both scripts execute in the same realm. The body
// script should either:
//   (a) use the names as-is via the shared cross-<script> hoisting,
//   (b) assign to them via `globalThis.api_ = ...`, or
//   (c) call `setupRecipesInit({ api: myApiFn, ... })` to inject a dep.
//
// Reference discipline inside this file: functions below reference these
// names only via `typeof x === 'function'`, which is safe because `typeof`
// on a declared-but-unassigned `let` returns `'undefined'` and does NOT
// throw a ReferenceError.
var api_;
var escapeHtml;
var flash_;
var updateRecipeSelection_;
var openPrepareMode_;

/**
 * Fetch the recipes from /api/recipes and render them as cards.
 * Each card supports selection (for grocery list generation) and
 * an optional Prepare-mode flow (Start cooking / Resume chip).
 *
 * Depends on `_loadPrepareProgress` (from prepare-storage.js) for
 * the Resume chip rendering, and `openPrepareMode` /
 * `updateRecipeSelection` (from the main inline script) for the
 * click handlers.
 */
async function refreshRecipes() {
  const list = document.getElementById('recipeListContainer');
  const { ok, data } = await fetchRecipes_();
  list.innerHTML = '';
  const counter = document.getElementById('recipeSelectedCount');
  if (counter) counter.textContent = '0';
  const countLabel = document.getElementById('recipeCountLabel');
  if (!ok || !Array.isArray(data) || data.length === 0) {
    list.innerHTML = '<div class="empty">No recipes yet.</div>';
    if (countLabel) countLabel.textContent = '0 recipes';
    return;
  }
  if (countLabel) countLabel.textContent = data.length + ' recipe' + (data.length !== 1 ? 's' : '');
  data.forEach(r => {
    const card = document.createElement('div');
    card.style.cssText = 'padding:14px 16px; border:1px solid var(--border); border-radius:10px; background:var(--bg-sunken); margin-bottom:10px;';
    // Render each ingredient as a bullet line with optional swaps.
    const _renderIng = function (i) {
      var swapsRow = (i.swap_options && i.swap_options.length)
        ? '<div style="color:var(--text-mute); font-size:.78rem; margin-top:2px;">Swaps: ' + escapeHtml_(i.swap_options.join(', ')) + '</div>'
        : '';
      if (i.display_text) {
        return '<div style="margin-top:4px;">• <strong>' + escapeHtml_(i.display_text) + '</strong>' + swapsRow + '</div>';
      }
      var requirement = i.quantity != null
        ? escapeHtml_(String(i.quantity)) + (i.unit ? ' ' + escapeHtml_(i.unit) : '') + ' — '
        : '';
      return '<div style="margin-top:4px;">• ' + requirement + '<strong>' + escapeHtml_(i.product_name) + '</strong>' + swapsRow + '</div>';
    };
    var allIngs = r.ingredients || [];
    var INGREDIENT_PREVIEW = 5;
    var showToggle = allIngs.length > INGREDIENT_PREVIEW;
    var visibleIngs = allIngs.slice(0, INGREDIENT_PREVIEW);
    var hiddenIngs = allIngs.slice(INGREDIENT_PREVIEW);
    var visibleHtml = visibleIngs.map(_renderIng).join('');
    var hiddenHtml = hiddenIngs.map(_renderIng).join('');
    var hiddenCount = hiddenIngs.length;

    // The hidden-ingredients container gets a deterministic id so the
    // toggle button can find it by id (rung_ing_extra_<recipeId>).
    var ingExtraId = 'rung_ing_extra_' + r.id;

    // Build an "imported extras" block when the recipe carries
    // url-import metadata (source_url, image_url, total_time,
    // instructions). Legacy hand-entered recipes have null fields
    // here and the block eval()s to an empty string.
    const safeUrl = r.source_url ? escapeHtml_(r.source_url) : '';
    const safeImg = r.image_url ? escapeHtml_(r.image_url) : '';
    const safeTime = r.total_time ? escapeHtml_(String(r.total_time)) : '';
    const safeInstr = r.instructions ? escapeHtml_(r.instructions) : '';

    // Resume chip payload (assembled into HTML if both step and
    // totalSteps are valid in-range integers AND step > 0 AND step <
    // totalSteps — i.e., there's actually progress to resume from).
    let chipHtml = '';
    const _savedProg = typeof _loadPrepareProgress === 'function' ? _loadPrepareProgress(r.id) : null;
    if (_savedProg
        && Number.isInteger(_savedProg.step)
        && Number.isInteger(_savedProg.totalSteps)
        && _savedProg.step > 0
        && _savedProg.step < _savedProg.totalSteps) {
      chipHtml = `<button type="button" class="prepare-resume-chip"
                       data-resume-id="${r.id}"
                       title="Resume cooking at step ${_savedProg.step + 1} of ${_savedProg.totalSteps}"
                       aria-label="Resume at step ${_savedProg.step + 1} of ${_savedProg.totalSteps}"
                       style="display:inline-flex; align-items:center; cursor:pointer; font:inherit; font-size:.7rem;
                              padding:3px 9px; border-radius:99px; background:var(--accent-soft); color:var(--accent);
                              border:1px solid rgba(45,191,110,.35); font-weight:700; letter-spacing:.04em;
                              transition:background 160ms ease, color 160ms ease, transform 160ms ease;">
                    ↻ Step ${_savedProg.step + 1}
                  </button>`;
    }

    const importExtras = (safeUrl || safeImg || safeTime || safeInstr) ? `
      ${safeImg ? `<div style="margin-top:10px;"><img src="${safeImg}" alt="" loading="lazy"
                 style="max-width:100%; max-height:140px; border-radius:6px;
                        border:1px solid var(--line, #2a2a2a);"/></div>` : ''}
      <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; font-size:.78rem;">
        ${safeTime ? `<span class="chip" style="background:var(--surface-2, #232323);
                    padding:3px 8px; border-radius:99px;">⏱ ${safeTime}</span>` : ''}
        ${safeUrl ? `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"
                      style="color:var(--accent, #6cf); text-decoration:none;">↗ source</a>` : ''}
      </div>
      ${safeInstr ? `<details style="margin-top:8px;">
                      <summary style="cursor:pointer; color:var(--text-mute); font-size:.82rem;">Instructions</summary>
                      <div style="margin-top:6px; padding-left:14px;
                                  white-space:pre-wrap; color:var(--text-dim); font-size:.82rem;
                                  border-left:2px solid var(--line, #2a2a2a);">${safeInstr}</div>
                    </details>
                    <div class="row" style="margin-top:10px; gap:8px; align-items:center; flex-wrap:wrap;">
                      <button type="button" class="btn is-primary prepare-start"
                              data-prepare-id="${r.id}"
                              style="padding:6px 14px; font-size:.82rem;">▶ Start cooking</button>
                      ${chipHtml}
                    </div>` : ''}
    ` : '';

    card.innerHTML = `
      <div class="row" style="justify-content:space-between; align-items:flex-start;">
        <label class="row" style="gap:8px; cursor:pointer; min-width:0;">
          <input type="checkbox" data-recipe="${r.id}" />
          <strong style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml_(r.title)}</strong>
        </label>
        <div class="row" style="flex-shrink:0; gap:4px;">
          <span style="color:var(--text-mute); font-size:.82rem;">${r.servings || 1} servings</span>
          ${r.can_delete ? `<button type="button" data-delete-id="${r.id}"
                  style="background:none; border:none; cursor:pointer;
                         color:var(--danger, #f43f5e); font-size:.85rem; padding:2px 6px;
                         border-radius:4px; transition:background 160ms ease;
                         line-height:1;"
                  title="Delete recipe: ${escapeHtml_(r.title)}">✕</button>` : ''}
        </div>
      </div>
      <div style="margin-top:8px; color:var(--text-dim); font-size:.85rem;">
        ${visibleHtml}
        ${showToggle ? `<div id="${ingExtraId}" style="display:none;">${hiddenHtml}</div>` : ''}
        ${showToggle ? `<button type="button" data-toggle-ing="${r.id}"
                        style="background:none; border:none; cursor:pointer;
                               color:var(--accent, #2dbf6e); font-size:.82rem; padding:4px 0 0 0;
                               font-family:inherit; transition:opacity 160ms ease;">
                          + ${hiddenCount} more ingredient${hiddenCount !== 1 ? 's' : ''}
                        </button>` : ''}
      </div>
      ${importExtras}
      <div class="row" style="margin-top:10px; gap:8px; align-items:center; flex-wrap:wrap;">
        <button type="button" data-grocery-id="${r.id}"
                style="padding:6px 14px; font-size:.82rem; border-radius:6px; cursor:pointer;
                       font-family:inherit; font-weight:600; transition:background 160ms ease, transform 160ms ease;
                       background:var(--accent-soft); color:var(--accent);
                       border:1px solid rgba(45,191,110,.3);">+ Add to Grocery</button>
      </div>
    `;
    const checkbox = card.querySelector('input[data-recipe]');
    if (checkbox && typeof updateRecipeSelection_ === 'function') {
      checkbox.addEventListener('change', updateRecipeSelection_);
    }
    const startBtn = card.querySelector('[data-prepare-id]');
    if (startBtn && typeof openPrepareMode_ === 'function') {
      startBtn.addEventListener('click', () => openPrepareMode_(r));
    }
    const resumeBtn = card.querySelector('[data-resume-id]');
    if (resumeBtn && typeof openPrepareMode_ === 'function') {
      resumeBtn.addEventListener('click', () => openPrepareMode_(r));
    }
    // Ingredient collapse/expand toggle.
    var toggleBtn = card.querySelector('[data-toggle-ing]');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', function () {
        var extra = document.getElementById('rung_ing_extra_' + r.id);
        if (!extra) return;
        var isHidden = extra.style.display === 'none';
        extra.style.display = isHidden ? 'block' : 'none';
        this.textContent = isHidden
          ? '▲ Show fewer'
          : '+ ' + hiddenCount + ' more ingredient' + (hiddenCount !== 1 ? 's' : '');
      });
    }

    const groceryBtn = card.querySelector('[data-grocery-id]');
    if (groceryBtn) {
      groceryBtn.addEventListener('click', function () {
        var cb = card.querySelector('input[data-recipe]');
        if (cb) { cb.checked = true; if (typeof updateRecipeSelection_ === 'function') updateRecipeSelection_(); }
        if (typeof flash_ === 'function') flash_('Added to grocery selection', 'success');
      });
    }

    const delBtn = card.querySelector('[data-delete-id]');
    if (delBtn) {
      delBtn.addEventListener('click', async () => {
        if (!confirm('Remove "' + (r.title || 'this recipe') + '" from your recipes?')) return;
        try {
          let resp;
          if (typeof api_ === 'function') {
            resp = await api_('DELETE', '/api/recipes/' + r.id);
          } else {
            const r2 = await fetch('/api/recipes/' + r.id, { method: 'DELETE' });
            resp = { ok: r2.ok, data: await r2.json().catch(function () { return {}; }) };
          }
          if (resp && resp.ok) {
            if (typeof flash_ === 'function') flash_('Recipe removed', 'success');
            await refreshRecipes();
          } else {
            if (typeof flash_ === 'function') flash_('Could not remove this recipe right now.', 'error');
          }
        } catch (_err) {
          if (typeof flash_ === 'function') flash_('Network issue while removing recipe.', 'error');
        }
      });
    }
    list.appendChild(card);
  });
}

/**
 * Internal fetch wrapper. Three-tier fallback:
 *   1. `api_` global (set by the inline body script's `api()` function).
 *   2. `_recipesMockFetch` test hook (set by tests/test_recipes.js via
 *      `SUT._setMockFetch(fn)`).
 *   3. Direct `globalThis.fetch` (used in browser fall-through).
 *
 * Branch 3 is defensive: throws if `globalThis.fetch` is unavailable,
 * so a missing-test-mock does NOT silently fire a real network request
 * in Node 18+ (which has built-in fetch and would otherwise hit
 * `/api/recipes` unintentionally).
 */
async function fetchRecipes_() {
  if (typeof api_ === 'function') return api_('GET', '/api/recipes');
  if (typeof _recipesMockFetch === 'function') {
    return _recipesMockFetch('GET', '/api/recipes', undefined);
  }
  if (typeof globalThis !== 'undefined' && typeof globalThis.fetch === 'function') {
    const r = await globalThis.fetch('/api/recipes', { method: 'GET' });
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  }
  throw new Error('recipes.js: no api function or fetch available; set up the mock or wire api() before calling refreshRecipes()');
}

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
 * Wire the Add Recipe form (POST /api/recipes) and the Import Recipe
 * form (POST /api/recipes/import with cache-status display). Call
 * this once from the inline body's init() AFTER the DOM is ready.
 *
 * @param {object} deps
 * @param {Function} deps.api                       fetch wrapper (path, body)
 * @param {Function} deps.flash                     toast notification (msg, kind)
 * @param {Function} deps.escapeHtml                HTML escape (defaults to module's escapeHtml_)
 * @param {Function} deps.refreshRecipes            recipe-list refresh (defaults to global refreshRecipes)
 */
function setupRecipesInit(deps) {
  deps = deps || {};
  const api = deps.api || ((m, p, b) => globalThis.api_(m, p, b));
  const flash = deps.flash || globalThis.flash;
  const escapeHtmlFn = deps.escapeHtml || escapeHtml_;
  // IMPORTANT: do NOT rename `refreshFn` back to `refreshRecipes`.
  // Local-scope shadowing of the module-level `function refreshRecipes()`
  // declaration breaks TDZ-safety: when `deps.refreshRecipes` is falsy
  // (e.g., not passed by the test harness in production-like scenarios),
  // the right side `refreshRecipes` evaluates the local const, which
  // is mid-initialization — ReferenceError. Keeping it as `refreshFn`
  // avoids the shadow entirely.
  const refreshFn = deps.refreshRecipes || refreshRecipes;

  // Bind module-scoped placeholders used by the recipe card handlers.
  // These are declared as `var` at the top of this module and referenced
  // via `typeof x === 'function'` guards in refreshRecipes(). Without this
  // binding, "Start cooking", checkbox-change handlers, and delete buttons
  // silently no-op.
  if (typeof deps.api === 'function') api_ = deps.api;
  if (typeof deps.flash === 'function') flash_ = deps.flash;
  if (typeof deps.openPrepareMode === 'function') openPrepareMode_ = deps.openPrepareMode;
  if (typeof deps.updateRecipeSelection === 'function') updateRecipeSelection_ = deps.updateRecipeSelection;

  // ----- Add Recipe (manual entry) -----
  const addForm = document.getElementById('addRecipeForm');
  if (addForm) {
    addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const title = document.getElementById('rTitle').value.trim();
      const servings = parseInt(document.getElementById('rServings').value, 10) || 4;
      const lines = document.getElementById('rIngredients').value.split('\n').map(l => l.trim()).filter(Boolean);
      if (!title) { if (flash) flash('Add a recipe title first.', 'error'); return; }
      const { ok, data } = await api('POST', '/api/recipes', { title, servings, ingredients: lines });
      if (!ok) { if (flash) flash('Could not add this recipe right now.', 'error'); return; }
      document.getElementById('rTitle').value = '';
      document.getElementById('rIngredients').value = '';
      if (flash) flash('Recipe added', 'success');
      await refreshFn();
    });
  }

  // ----- Import Recipe from URL -----
  const importForm = document.getElementById('importRecipeForm');
  if (!importForm) return;
  importForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = document.getElementById('importRecipeUrl');
    const status = document.getElementById('importRecipeStatus');
    const btn = document.getElementById('importRecipeBtn');
    const url = (input.value || '').trim();
    if (!url) {
      status.textContent = 'Please paste a URL first.';
      status.style.color = 'var(--text-mute)';
      return;
    }
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Importing…';
    status.textContent = 'Fetching recipe from ' + url + ' …';
    status.style.color = 'var(--text-mute)';
    try {
      const resp = await api('POST', '/api/recipes/import', { url });
      // The inline-body `api()` wrapper returns `{ok, status, data: <body>}`.
      // We must (1) gate on `resp.ok` to distinguish success from
      // server-error responses (the pre-extraction handler silently went
      // through the success path on a 5xx, rendering a misleading
      // "✓ Imported 'Recipe'" while the import actually failed), and
      // (2) unwrap to the body before reading response fields (the
      // pre-extraction handler had THIS bug too — it accessed
      // `data.recipe.title` directly off the wrapper, which threw
      // `TypeError: Cannot read property 'title' of undefined`).
      if (!resp.ok) {
        const errMsg = 'We could not import that recipe right now.';
        throw new Error(errMsg);
      }
      const data = resp && resp.data ? resp.data : {};
      const title = data.recipe && data.recipe.title ? data.recipe.title : 'Recipe';
      let cacheNote = '';
      const cache = data.cache ? data.cache : null;
      if (cache && cache.status === 'ok') {
        if (cache.hit) {
          const ageMin = Math.max(1, Math.round((cache.age_seconds || 0) / 60));
          const unit = ageMin < 60 ? 'min' : 'h';
          const age = ageMin < 60 ? ageMin : (ageMin / 60).toFixed(1);
          cacheNote = ' · cached ' + age + ' ' + unit + ' ago';
        } else {
          cacheNote = ' · freshly fetched';
        }
      }
      status.textContent = '✓ Imported "' + title + '"' + cacheNote + '. Refreshing list…';
      status.style.color = 'var(--accent, #6cf)';
      input.value = '';
      await refreshFn();
    } catch (err) {
      const msg = (err && err.message) ? err.message : (typeof err === 'string' ? err : 'Import failed.');
      status.textContent = '✗ ' + msg;
      status.style.color = 'crimson';
      if (flash) flash(msg, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });
}

// ----------------------------------------------------------------------------
// TEST HOOKS (Node only — browser never touches these).
// ----------------------------------------------------------------------------
// `_recipesMockFetch` is set only by the smoke test harness via
// `SUT._setMockFetch(fn)` (see /tmp/extract_recipes_module.py below).
// It exists in module scope so `fetchRecipes_` finds it via the
// `typeof _recipesMockFetch === 'function'` fallback branch. The
// browser never reads or writes this variable (it has no DOM/Fetch
// mocks in production).
let _recipesMockFetch;

/**
 * Setup keyword recipe search — wires the search button and Enter key
 * on the search input to fetch from /api/recipes/search?q=<query> and
 * render results into #recipeResultsList.
 *
 * This function is idempotent: calling it multiple times will not
 * duplicate listeners because the previous listeners are tracked via
 * the `_searchListenersAttached` flag on each element.
 *
 * Dependencies (resolved at call-time):
 *   - `api`  fetch wrapper (method, path, body) => { ok, status, data }
 *   - `escapeHtml` HTML-escape function
 *   - `flash`  toast notification
 *
 * @param {object} deps
 * @param {Function} deps.api                        fetch wrapper
 * @param {Function} [deps.escapeHtml]                HTML escape
 * @param {Function} [deps.flash]                     toast notification
 */
function setupRecipesSearch(deps) {
  deps = deps || {};
  const api = deps.api || globalThis.api;
  const esc = deps.escapeHtml || escapeHtml_;
  const flash = deps.flash || globalThis.flash;

  var searchBtn = document.getElementById('searchRecipesBtn');
  var searchInput = document.getElementById('recipeSearchInput');
  if (!searchBtn || !searchInput) return;

  // Guard: skip if already wired
  if (searchBtn._searchListenersAttached) return;
  searchBtn._searchListenersAttached = true;

  async function doSearch() {
    var q = (searchInput.value || '').trim();
    if (!q) {
      if (flash) flash('Enter a search term first.', 'error');
      return;
    }
    var list = document.getElementById('recipeResultsList');
    if (!list) return;

    list.innerHTML = '<div class="empty">Searching for &quot;' + esc(q) + '&quot;&hellip;</div>';

    var resp;
    if (typeof api === 'function') {
      resp = await api('GET', '/api/recipes/search?q=' + encodeURIComponent(q));
    } else {
      try {
        var r = await fetch('/api/recipes/search?q=' + encodeURIComponent(q));
        var data = await r.json().catch(function () { return {}; });
        resp = { ok: r.ok, status: r.status, data: data };
      } catch (err) {
        resp = { ok: false, status: 0, data: { error: err.message || 'Network error' } };
      }
    }

    if (!resp || !resp.ok) {
      list.innerHTML = '<div class="empty" style="color:var(--danger);border-color:rgba(244,63,94,.3);">We could not run that search right now.</div>';
      if (flash) flash('Could not search recipes right now.', 'error');
      return;
    }

    var recipes = Array.isArray(resp.data) ? resp.data : (resp.data.results || []);
    if (!recipes.length) {
      list.innerHTML = '<div class="empty">No recipes found for &quot;' + esc(q) + '&quot;. Try a different search term.</div>';
      return;
    }

    list.innerHTML = '';
    recipes.forEach(function (r) {
      var card = document.createElement('div');
      card.style.cssText = 'padding:14px 16px; border:1px solid var(--border); border-radius:10px; background:var(--bg-sunken); margin-bottom:10px;';

      var sourceBadge = r.source === 'themealdb'
        ? '<span style="font-size:0.7rem;padding:2px 8px;border-radius:99px;background:var(--accent-soft);color:var(--accent);border:1px solid rgba(45,191,110,.3);font-weight:600;">TheMealDB</span>'
        : '<span style="font-size:0.7rem;padding:2px 8px;border-radius:99px;background:var(--bg-sunken);color:var(--text-mute);border:1px solid var(--border);font-weight:600;">Local</span>';

      var imgHtml = r.image_url
        ? '<img src="' + esc(r.image_url) + '" alt="" loading="lazy" style="width:60px;height:60px;border-radius:8px;object-fit:cover;border:1px solid var(--border);flex-shrink:0;"/>'
        : '';
      var categoryHtml = r.category
        ? '<div style="color:var(--text-mute);font-size:0.78rem;margin-top:4px;">' + esc(r.category) + (r.area ? ' \u00b7 ' + esc(r.area) : '') + '</div>'
        : '';
      var descHtml = r.description
        ? '<div style="color:var(--text-dim);font-size:0.82rem;margin-top:6px;">' + esc(r.description.substring(0, 200)) + '</div>'
        : '';

      card.innerHTML = '<div class="row" style="gap:12px;align-items:flex-start;">'
        + imgHtml
        + '<div style="flex:1;min-width:0;">'
        + '<div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap;">'
        + '<strong style="font-size:0.95rem;">' + esc(r.title) + '</strong>'
        + sourceBadge
        + '</div>'
        + categoryHtml
        + descHtml
        + '</div>'
        + '</div>';

      list.appendChild(card);
    });
  }

  searchBtn.addEventListener('click', doSearch);
  searchInput.addEventListener('keypress', function (e) {
    if (e.key === 'Enter') doSearch();
  });
}

// ----------------------------------------------------------------------------
// CONDITIONAL COMMONJS EXPORT — Node.js testability.
// ----------------------------------------------------------------------------
// The browser has no `module` global, so the `typeof` guard is `false`
// and this block is skipped at runtime in production. In Node (test
// runner, smoke tests), it exposes the helpers as a requireable object
// so the API contract can be verified without spinning up a browser.
// See tests/test_recipes.js.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    refreshRecipes,
    setupRecipesInit,
    setupRecipesSearch,
    // Internal hooks for tests (NOT used by the browser glue):
    _setMockFetch: (fn) => { _recipesMockFetch = fn; },
    _resetMockFetch: () => { _recipesMockFetch = null; },
    _resetApi: () => { api_ = undefined; },
  };
}
