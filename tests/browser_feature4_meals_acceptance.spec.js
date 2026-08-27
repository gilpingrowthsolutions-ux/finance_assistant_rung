const { test, expect } = require('@playwright/test');
if (process.env.RUNG_PLAYWRIGHT_CHROMIUM) test.use({ launchOptions: { executablePath: process.env.RUNG_PLAYWRIGHT_CHROMIUM } });
const ROOT = process.env.RUNG_BROWSER_ROOT || 'http://127.0.0.1:5051';
const A = {email:'feature4-browser@example.com', password:'browser-pass-123'};
const B = {email:'feature4-browser-b@example.com', password:'browser-pass-456'};

async function api(page, method, path, body) {
  return page.evaluate(async ({method,path,body}) => {
    const response = await fetch(path, {method, headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const text = await response.text(); let data;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
    return {status:response.status, data};
  }, {method,path,body});
}
async function login(page, account=A) {
  await page.goto(ROOT+'/', {waitUntil:'networkidle'});
  const result = await api(page, 'POST', '/api/auth/login', account);
  expect(result.status).toBe(200); expect(result.data.authenticated).toBe(true);
  await page.reload({waitUntil:'networkidle'});
  await expect(page.locator('#onboardingDialog')).not.toBeVisible();
  const state = await api(page, 'GET', '/api/onboarding/state');
  expect(state.status).toBe(200); expect(state.data.show_onboarding).toBe(false); expect(state.data.readiness.complete).toBe(true);
}
async function openMeals(page) {
  await page.locator('[data-target="recipes"]').click();
  await expect(page.locator('#recipes')).toBeVisible();
  await expect(page.locator('#recipeListContainer')).not.toContainText('Loading your recipe library');
}
function byTitle(rows, title) { const value=rows.find(r=>r.title===title); expect(value, title).toBeTruthy(); return value; }
function ingredient(recipe, base) { const value=recipe.ingredients.find(i=>i.clean_keyword===base); expect(value, base).toBeTruthy(); return value; }

test.describe.configure({mode:'serial'});
test('Feature 4 Meals browser matrix: ownership, plan, tombstones, and Shopping boundary', async ({page}) => {
  const consoleErrors=[], pageErrors=[], failed=[], requests=[], responses=[];
  page.on('console', m => { if (m.type()==='error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('requestfailed', r => failed.push({url:r.url(), failure:r.failure()}));
  page.on('request', r => { if (r.url().startsWith(ROOT+'/api/')) requests.push({method:r.method(),path:new URL(r.url()).pathname}); });
  page.on('response', r => { if (r.url().startsWith(ROOT+'/api/')) responses.push({method:r.request().method(),path:new URL(r.url()).pathname,status:r.status()}); });
  await page.setViewportSize({width:1440,height:1000}); await login(page,A); await openMeals(page);

  // Empty means history is not a current selection surface; list shows only A + canonical authority.
  await expect(page.locator('#mealsActiveRecipes')).toContainText('No active recipes yet');
  let catalog=await api(page,'GET','/api/recipes'); expect(catalog.status).toBe(200);
  expect(catalog.data.map(r=>r.title)).toEqual(expect.arrayContaining(['Canonical Rice Bowl','Historical Private']));
  expect(catalog.data.map(r=>r.title)).not.toEqual(expect.arrayContaining(['Prior Cycle Soup','Quarantined Legacy','B Private']));
  const canonical=byTitle(catalog.data,'Canonical Rice Bowl'), historical=byTitle(catalog.data,'Historical Private');
  expect(ingredient(canonical,'rice')).toMatchObject({product_name:'2 cups rice',quantity:2,unit:'cup'});

  // Owner creates private data only through the served UI.
  await page.locator('#openRecipeCreateDialog').click(); await page.locator('#rTitle').fill('Browser Private'); await page.locator('#rServings').fill('6'); await page.locator('#rIngredients').fill('2 cups lentils'); await page.locator('#addRecipeForm button[type=submit]').click();
  await expect(page.locator('#recipeListContainer')).toContainText('Browser Private'); await page.locator('#closeRecipeCreateDialog').click();
  catalog=await api(page,'GET','/api/recipes'); const privateRecipe=byTitle(catalog.data,'Browser Private');
  expect(privateRecipe).toMatchObject({can_delete:true,can_edit:true,servings:6,instructions:''}); expect(ingredient(privateRecipe,'lentils')).toMatchObject({product_name:'2 cups lentils',quantity:2,unit:'cup'}); expect(canonical.can_delete).toBe(false);

  // Detail fidelity (including numeric quantity + unit), then reload persistence.
  await page.locator('.recipe-browse-card').filter({hasText:'Browser Private'}).getByRole('button',{name:'View Recipe'}).click();
  await expect(page.locator('#recipeDetailTitle')).toHaveText('Browser Private'); await expect(page.locator('#recipeDetailMeta')).toHaveText('6 servings'); await expect(page.locator('#recipeDetailIngredients')).toHaveText('2 cups lentils'); await expect(page.locator('#recipeDetailInstructionsWrap')).toBeHidden(); await page.locator('#closeRecipeDetailDialog').click();
  await page.locator('.recipe-browse-card').filter({hasText:'Canonical Rice Bowl'}).getByRole('button',{name:'View Recipe'}).click();
  await expect(page.locator('#recipeDetailTitle')).toHaveText('Canonical Rice Bowl'); await expect(page.locator('#recipeDetailMeta')).toHaveText('4 servings'); await expect(page.locator('#recipeDetailIngredients')).toHaveText('2 cups rice'); await expect(page.locator('#recipeDetailInstructions')).toHaveText('Cook Canonical Rice Bowl gently.'); await expect(page.locator('#recipeDetailDeleteAction')).toBeHidden(); await page.locator('#closeRecipeDetailDialog').click();
  await page.reload({waitUntil:'networkidle'}); await openMeals(page); await page.locator('.recipe-browse-card').filter({hasText:'Browser Private'}).getByRole('button',{name:'View Recipe'}).click(); await expect(page.locator('#recipeDetailIngredients')).toHaveText('2 cups lentils'); await page.locator('#closeRecipeDetailDialog').click();

  // UI activation + repeated API activation proves idempotent current-cycle selection.
  await page.locator('.recipe-browse-card').filter({hasText:'Canonical Rice Bowl'}).getByRole('button',{name:'Add to This Pay Period'}).click(); await expect(page.locator('#mealsActiveRecipes')).toContainText('Canonical Rice Bowl');
  await page.locator('.recipe-browse-card').filter({hasText:'Browser Private'}).getByRole('button',{name:'Add to This Pay Period'}).click(); await expect(page.locator('#mealsActiveRecipes')).toContainText('Browser Private');
  let plan=await api(page,'GET','/api/meal-plan'); expect(plan.data.recipe_ids.slice().sort((a,b)=>a-b)).toEqual([canonical.id,privateRecipe.id].sort((a,b)=>a-b));
  const repeated=await api(page,'POST','/api/meal-plan',{add:[privateRecipe.id]}); expect(repeated.status).toBe(200); expect(repeated.data.recipe_ids.filter(id=>id===privateRecipe.id)).toHaveLength(1);
  await expect(page.locator('#mealsActiveRecipes')).toContainText('Canonical Rice Bowl'); await expect(page.locator('#mealsActiveRecipes')).toContainText('Browser Private'); await expect(page.locator('#mealsActiveRecipes')).not.toContainText('Prior Cycle Soup');
  const beforeExpander=await api(page,'GET','/api/meal-plan'); await page.locator('#shoppingActiveRecipesPanel').evaluate(n=>n.open=true); await expect(page.locator('#shoppingActiveRecipesPanel')).toContainText('Canonical Rice Bowl'); expect((await api(page,'GET','/api/meal-plan')).data.recipe_ids).toEqual(beforeExpander.data.recipe_ids);
  plan=await api(page,'GET','/api/meal-plan'); expect(ingredient(byTitle(plan.data.recipes,'Canonical Rice Bowl'),'rice')).toMatchObject({quantity:2,unit:'cup'}); expect(ingredient(byTitle(plan.data.recipes,'Browser Private'),'lentils')).toMatchObject({quantity:2,unit:'cup'}); expect(JSON.stringify(plan.data)).not.toMatch(/beans|barley|oats|quarantined/i);

  // Exact expected active-delete conflict leaves all current authority intact.
  const activeDelete=await api(page,'DELETE',`/api/recipes/${privateRecipe.id}`); expect(activeDelete.status).toBe(409); expect(activeDelete.data.error).toMatch(/Remove this recipe from the current plan/i); expect((await api(page,'GET','/api/meal-plan')).data.recipe_ids).toEqual(expect.arrayContaining([privateRecipe.id]));

  // Authenticate B in the same real browser and directly probe A's ID.
  expect((await api(page,'POST','/api/auth/logout')).status).toBe(200); await login(page,B); await openMeals(page);
  const bCatalog=await api(page,'GET','/api/recipes'); expect(bCatalog.data.map(r=>r.title)).toEqual(expect.arrayContaining(['Canonical Rice Bowl','B Private'])); expect(bCatalog.data.map(r=>r.title)).not.toEqual(expect.arrayContaining(['Browser Private','Historical Private','Quarantined Legacy']));
  expect((await api(page,'GET','/api/recipes/search?q=Browser%20Private')).data.map(r=>r.title)).not.toContain('Browser Private');
  const bBefore=await api(page,'GET','/api/meal-plan');
  const bRead=await api(page,'POST','/api/recipes/generate',{recipe_ids:[privateRecipe.id]}), bDelete=await api(page,'DELETE',`/api/recipes/${privateRecipe.id}`), bActivate=await api(page,'POST','/api/meal-plan',{add:[privateRecipe.id]}), bEdit=await api(page,'PATCH',`/api/recipes/${privateRecipe.id}`,{title:'leak'});
  expect(bRead.status).toBe(404); expect(bDelete.status).toBe(404); expect(bActivate.status).toBe(404); expect(bEdit.status).toBe(405); expect((await api(page,'GET','/api/meal-plan')).data).toEqual(bBefore.data); expect(JSON.stringify((await api(page,'GET','/api/meal-plan')).data)).not.toMatch(/lentils|Browser Private/i);

  // Switchback proves B's denied requests made no A mutation.
  await api(page,'POST','/api/auth/logout'); await login(page,A); await openMeals(page); expect((await api(page,'GET','/api/meal-plan')).data.recipe_ids.slice().sort((a,b)=>a-b)).toEqual([canonical.id,privateRecipe.id].sort((a,b)=>a-b));

  // Deactivate visibly then confirm tombstone visibly; canonical stays protected.
  await page.locator('.meals-active-card').filter({hasText:'Browser Private'}).getByRole('button',{name:'Remove'}).click(); await expect(page.locator('#mealsActiveRecipes')).not.toContainText('Browser Private'); expect((await api(page,'GET','/api/meal-plan')).data.recipe_ids).toEqual([canonical.id]);
  await page.locator('.recipe-browse-card').filter({hasText:'Browser Private'}).getByRole('button',{name:'View Recipe'}).click(); await expect(page.locator('#recipeDetailDeleteAction')).toBeVisible(); page.once('dialog',d=>d.accept()); await page.locator('#recipeDetailDeleteAction').click(); await expect(page.locator('#recipeListContainer')).not.toContainText('Browser Private'); expect((await api(page,'POST','/api/meal-plan',{add:[privateRecipe.id]})).status).toBe(404);
  // Historical-only private tombstone must remain a historical plan row but is now hidden/inert.
  await page.locator('.recipe-browse-card').filter({hasText:'Historical Private'}).getByRole('button',{name:'View Recipe'}).click(); page.once('dialog',d=>d.accept()); await page.locator('#recipeDetailDeleteAction').click(); await expect(page.locator('#recipeListContainer')).not.toContainText('Historical Private'); expect((await api(page,'POST','/api/meal-plan',{add:[historical.id]})).status).toBe(404);
  expect((await api(page,'DELETE',`/api/recipes/${canonical.id}`)).status).toBe(404);

  // Handoff is navigation only: no plan, selected-store, or cart mutation and no GPS/store request.
  const beforeHandoffPlan=await api(page,'GET','/api/meal-plan'), beforeStore=await api(page,'GET','/api/settings/current-location'), beforeCart=await api(page,'GET','/api/grocery');
  await page.locator('#mealsGoShopping').click(); await expect(page.locator('#shopping')).toBeVisible(); await expect(page.locator('#shoppingActiveRecipesPanel')).toContainText('Canonical Rice Bowl'); expect((await api(page,'GET','/api/meal-plan')).data).toEqual(beforeHandoffPlan.data); expect((await api(page,'GET','/api/settings/current-location')).data.selected_store).toEqual(beforeStore.data.selected_store); expect((await api(page,'GET','/api/grocery')).data).toEqual(beforeCart.data);
  await page.screenshot({path:'/tmp/rung-feature4-meals-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844}); await page.locator('#mobileMoreBtn').click(); await page.locator('.mobile-more-destination',{hasText:'Meals'}).click(); await expect(page.locator('#recipes')).toBeVisible(); await expect(page.locator('#mealsActiveRecipes')).toContainText('Canonical Rice Bowl'); expect(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth)).toBe(true); await page.screenshot({path:'/tmp/rung-feature4-meals-mobile.png',fullPage:true});

  const expectedDenial = r => (r.method==='DELETE' && r.path===`/api/recipes/${privateRecipe.id}` && (r.status===409 || r.status===404)) || (r.method==='POST' && r.path==='/api/recipes/generate' && r.status===404) || (r.method==='POST' && r.path==='/api/meal-plan' && r.status===404) || (r.method==='PATCH' && r.path===`/api/recipes/${privateRecipe.id}` && r.status===405) || (r.method==='DELETE' && r.path===`/api/recipes/${canonical.id}` && r.status===404);
  const denials=responses.filter(r=>r.status>=400); expect(denials.length).toBeGreaterThan(0); expect(denials.every(expectedDenial)).toBe(true);
  // Chromium emits generic resource errors without a URL.  Their number is
  // therefore reconciled to the exact observed method/path/status denial set,
  // rather than globally ignoring a status-code substring.
  expect(consoleErrors.every(message=>/status of (409|404|405) \(/.test(message))).toBe(true); expect(consoleErrors).toHaveLength(denials.length);
  expect(requests.filter(r=>r.method==='POST'&&r.path==='/api/location/select')).toHaveLength(0); expect(failed).toEqual([]); expect(pageErrors).toEqual([]);
});
