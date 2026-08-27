"""Authoritative ordinary-household recipe visibility and authorization."""

from __future__ import annotations

from sqlalchemy import and_, or_

from models import Recipe


def visible_recipe_filter(household_id: int):
    """SQL predicate for recipes a household may browse, read, or activate."""
    return or_(
        and_(Recipe.recipe_scope == Recipe.SCOPE_CANONICAL, Recipe.tombstoned_at.is_(None)),
        and_(
            Recipe.recipe_scope == Recipe.SCOPE_HOUSEHOLD_PRIVATE,
            Recipe.household_id == int(household_id),
            Recipe.tombstoned_at.is_(None),
        ),
    )


def visible_recipe_query(household_id: int):
    return Recipe.query.filter(visible_recipe_filter(household_id))


def visible_recipe_by_id(household_id: int, recipe_id: int):
    """Return a visible recipe or None, intentionally avoiding enumeration."""
    return visible_recipe_query(household_id).filter(Recipe.id == int(recipe_id)).first()


def mutable_private_recipe_by_id(household_id: int, recipe_id: int):
    """Only the owning household may mutate its private recipes."""
    return Recipe.query.filter(
        Recipe.id == int(recipe_id),
        Recipe.recipe_scope == Recipe.SCOPE_HOUSEHOLD_PRIVATE,
        Recipe.household_id == int(household_id),
        Recipe.tombstoned_at.is_(None),
    ).first()


def canonical_recipe(**kwargs):
    """Explicit construction helper for trusted catalog/seed code only."""
    return Recipe(recipe_scope=Recipe.SCOPE_CANONICAL, household_id=None, **kwargs)
