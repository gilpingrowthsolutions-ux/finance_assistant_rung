"""Focused regression coverage for mixed recipe ownership and quarantine."""
from __future__ import annotations

import hashlib
import hmac
import os

os.environ['RUNG_DB_PATH'] = ':memory:'

import pytest

from app import app, db
from models import Account, Household, MealPlanItem, Recipe, RecipeIngredient
from services.recipe_requirements import active_recipe_requirements
from tests.meal_plan_support import install_current_cycle


@pytest.fixture()
def households(monkeypatch):
    monkeypatch.setenv('RUNG_HOUSEHOLD_CONTEXT_SECRET', 'recipe-ownership-test')
    install_current_cycle(monkeypatch)
    app.testing = True
    with app.app_context():
        db.drop_all(); db.create_all()
        a = Household(public_id='11111111-1111-1111-1111-111111111111', legacy_scope_key='a')
        b = Household(public_id='22222222-2222-2222-2222-222222222222', legacy_scope_key='b')
        db.session.add_all([a, b]); db.session.flush()
        db.session.add_all([Account(household_id=a.id), Account(household_id=b.id)])
        canonical = Recipe(title='Catalog Pasta', recipe_scope=Recipe.SCOPE_CANONICAL)
        private_a = Recipe(title='A Secret', recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=a.id)
        private_b = Recipe(title='B Secret', recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=b.id)
        quarantined = Recipe(title='Test Chicken Bowl', recipe_scope=Recipe.SCOPE_LEGACY_QUARANTINED)
        db.session.add_all([canonical, private_a, private_b, quarantined]); db.session.flush()
        for recipe in (canonical, private_a, private_b, quarantined):
            db.session.add(RecipeIngredient(recipe_id=recipe.id, product_name='2 cups rice', clean_keyword='rice', quantity=2, unit='cup'))
        db.session.commit()
        return (a.id, a.public_id), (b.id, b.public_id), canonical.id, private_a.id, private_b.id, quarantined.id


def _headers(household):
    _id, public_id = household
    sig = hmac.new(b'recipe-ownership-test', public_id.encode(), hashlib.sha256).hexdigest()
    return {'X-Household-Id': public_id, 'X-Household-Signature': sig}


def test_mixed_visibility_mutation_activation_and_requirements(households):
    a, b, canonical_id, private_a_id, private_b_id, quarantine_id = households
    client = app.test_client()
    a_headers, b_headers = _headers(a), _headers(b)

    assert {r['id'] for r in client.get('/api/recipes', headers=a_headers).get_json()} == {canonical_id, private_a_id}
    assert {r['id'] for r in client.get('/api/recipes', headers=b_headers).get_json()} == {canonical_id, private_b_id}
    assert client.delete(f'/api/recipes/{canonical_id}', headers=a_headers).status_code == 404
    assert client.delete(f'/api/recipes/{private_a_id}', headers=b_headers).status_code == 404
    assert client.delete(f'/api/recipes/{quarantine_id}', headers=a_headers).status_code == 404
    assert client.post('/api/meal-plan', headers=b_headers, json={'add': [private_a_id]}).status_code == 404
    assert client.post('/api/meal-plan', headers=a_headers, json={'add': [quarantine_id]}).status_code == 404
    assert client.post('/api/meal-plan', headers=a_headers, json={'add': [canonical_id]}).status_code == 200
    assert client.post('/api/meal-plan', headers=a_headers, json={'add': [canonical_id]}).get_json()['recipe_ids'] == [canonical_id]
    with app.app_context():
        assert [r.source_recipe_id for r in active_recipe_requirements(a[0])] == [canonical_id]
        assert Recipe.query.filter_by(id=quarantine_id, recipe_scope=Recipe.SCOPE_LEGACY_QUARANTINED).count() == 1


def test_create_scope_cannot_be_client_elevated(households):
    a, _b, _canonical_id, _private_a_id, _private_b_id, _quarantine_id = households
    client = app.test_client()
    response = client.post('/api/recipes', headers=_headers(a), json={
        'title': 'My Dinner', 'ingredients': ['1 lb chicken'],
        'recipe_scope': 'canonical', 'household_id': 999999,
    })
    assert response.status_code == 200
    with app.app_context():
        recipe = Recipe.query.get(response.get_json()['id'])
        assert recipe.recipe_scope == Recipe.SCOPE_HOUSEHOLD_PRIVATE
        assert recipe.household_id == a[0]


def test_unclassified_constructor_is_not_accidentally_global(households):
    a, _b, _canonical_id, _private_a_id, _private_b_id, _quarantine_id = households
    with app.app_context():
        accidental = Recipe(title='Unclassified constructor')
        db.session.add(accidental)
        db.session.commit()
        accidental_id = accidental.id
        assert accidental.recipe_scope == Recipe.SCOPE_LEGACY_QUARANTINED
    rows = app.test_client().get('/api/recipes', headers=_headers(a)).get_json()
    assert accidental_id not in {row['id'] for row in rows}


def test_active_private_delete_requires_visible_deactivation_then_tombstones(households):
    a, _b, _canonical_id, private_a_id, _private_b_id, _quarantine_id = households
    client = app.test_client()
    headers = _headers(a)
    assert client.post('/api/meal-plan', headers=headers, json={'add': [private_a_id]}).status_code == 200
    with app.app_context():
        assert [row.source_recipe_id for row in active_recipe_requirements(a[0])] == [private_a_id]
    blocked = client.delete(f'/api/recipes/{private_a_id}', headers=headers)
    assert blocked.status_code == 409
    assert 'Remove this recipe' in blocked.get_json()['error']
    assert client.get('/api/meal-plan', headers=headers).get_json()['recipe_ids'] == [private_a_id]
    with app.app_context():
        assert Recipe.query.get(private_a_id) is not None
        assert MealPlanItem.query.filter_by(household_id=a[0], recipe_id=private_a_id).count() == 1
    assert client.post('/api/meal-plan', headers=headers, json={'remove': [private_a_id]}).status_code == 200
    with app.app_context():
        assert active_recipe_requirements(a[0]) == []
    assert client.delete(f'/api/recipes/{private_a_id}', headers=headers).status_code == 200
    with app.app_context():
        recipe = Recipe.query.get(private_a_id)
        assert recipe is not None and recipe.tombstoned_at is not None
        assert RecipeIngredient.query.filter_by(recipe_id=private_a_id).count() == 1
        assert MealPlanItem.query.filter_by(household_id=a[0], recipe_id=private_a_id).count() == 0
