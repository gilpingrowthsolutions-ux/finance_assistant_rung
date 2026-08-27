"""Deterministic adapter: persisted active recipes -> retail shopping requirements.

This module is the *only* bridge between the pay-period meal plan
(``MealPlanItem`` — the Package 5 active-recipe authority) and the verified
retail cart (``services.retail.cart.build_verified_retail_cart``).

For every ingredient of every active recipe it produces one
``ShoppingRequirement`` that:

  * preserves the original source text (``product_name``),
  * preserves the Package 6 parsed ``quantity``/``unit`` verbatim (including
    ``None`` for genuinely unknown requirements such as "salt to taste"), and
  * derives a clean retail base query that excludes any quantity/unit text.

Duplicate ingredients are deliberately *not* aggregated here. There is no
authoritative duplicate-ingredient aggregation policy yet, so each ingredient
keeps its own provenance (source recipe id, title, and source text) instead of
silently collapsing into one requirement.
"""

from __future__ import annotations

import re
from typing import Any

from models import MealPlanItem, Recipe, RecipeIngredient
from services.recipe_access import visible_recipe_filter
from services.retail.base import ShoppingRequirement


_QUANTITY_TOKENS = (
    r"\d+(?:\.\d+)?(?:/\d+)?|[¼½¾⅓⅔⅛⅜⅝⅞]|"
    r"one|two|three|four|five|six|seven|eight|nine|ten"
)
_UNIT_TOKENS = (
    r"tablespoons?|teaspoons?|tbsp|tsp|fluid\s+ounces?|fl\s*oz|ounces?|oz|"
    r"pounds?|lbs?|grams?|kilograms?|kg|milliliters?|liters?|quarts?|pints?|"
    r"gallons?|cups?|cans?|cloves?|heads?|stalks?|sprigs?|pinches?|dashes?|"
    r"bunches?|pieces?|slices?|jars?|bottles?|bags?|boxes?|packages?|packs?|"
    r"sticks?|dozen|items?|each|ea|eggs?"
)
_LEADING_REQUIREMENT_RE = re.compile(
    rf"^(?:(?:{_QUANTITY_TOKENS})\s+(?:{_UNIT_TOKENS})\s+(?:of\s+)?|"
    rf"(?:{_QUANTITY_TOKENS})\s+|"
    rf"(?:a|an)\s+(?:{_UNIT_TOKENS})\s+(?:of\s+)?)",
    re.IGNORECASE,
)
_TRAILING_NOTES_RE = re.compile(
    r",?\s*(?:to taste|for garnish|for serving|for drizzling|as needed|"
    r"or as needed|optional|divided|or more|more if desired).*$",
    re.IGNORECASE,
)
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:and|or|with|plus|of|for|in|a|an|the)\s+",
    re.IGNORECASE,
)


def _strip_leading_quantity_unit(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _LEADING_REQUIREMENT_RE.sub("", text, count=1).strip()
    return text


def derive_recipe_base_item(*, product_name: str, clean_keyword: str = "") -> str:
    """Derive a clean retail query from a persisted recipe ingredient.

    Prefers the Package 6 ``clean_keyword`` (which already excludes quantity
    and unit text), normalizes it to space-separated words, then defensively
    strips any residual quantity/unit prefix and trailing recipe notes. Falls
    back to the raw ``product_name`` when no clean keyword exists.
    """
    candidate = " ".join(str(clean_keyword or "").strip().lower().replace("_", " ").split())
    if not candidate:
        candidate = " ".join(str(product_name or "").strip().lower().split())

    candidate = _strip_leading_quantity_unit(candidate)
    candidate = _TRAILING_NOTES_RE.sub("", candidate).strip()
    candidate = " ".join(candidate.split())
    candidate = _LEADING_CONNECTOR_RE.sub("", candidate, count=1)
    return " ".join(candidate.split()).strip()


def _as_quantity(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_unit(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def requirement_from_ingredient(
    ingredient: RecipeIngredient,
    recipe: Recipe,
) -> ShoppingRequirement:
    """Convert one persisted ingredient row into a provenance-bearing requirement."""
    source_text = str(ingredient.product_name or "").strip()
    base_item = derive_recipe_base_item(
        product_name=source_text,
        clean_keyword=str(ingredient.clean_keyword or ""),
    )
    return ShoppingRequirement(
        item_name=source_text or base_item or "recipe ingredient",
        base_item=base_item or source_text or "recipe ingredient",
        quantity=_as_quantity(ingredient.quantity),
        unit=_as_unit(ingredient.unit),
        source_kind="recipe",
        source_recipe_id=int(recipe.id),
        source_recipe_title=str(recipe.title or "").strip() or None,
        source_text=source_text or None,
    )


def active_recipe_requirements(household_id: int) -> list[ShoppingRequirement]:
    """Build requirements for every persisted active recipe in *household_id*.

    Iterates ``MealPlanItem`` in insertion order, then each recipe's
    ingredients in persisted order, so repeated cart generation is stable and
    never mutates or duplicates persisted state.
    """
    # Keep the resolver in the application boundary where canonical income
    # schedule inference lives; never rederive historical identity here.
    from app import _current_meal_plan_cycle
    from services.meal_plan import current_plan_query
    plan_items = (
        current_plan_query(household_id, _current_meal_plan_cycle())
        .order_by(MealPlanItem.created_at.asc(), MealPlanItem.id.asc())
        .all()
    )
    if not plan_items:
        return []

    recipe_ids = [item.recipe_id for item in plan_items]
    recipes = {
        r.id: r for r in Recipe.query.filter(
            Recipe.id.in_(recipe_ids), visible_recipe_filter(household_id),
        ).all()
    }
    visible_ids = list(recipes)
    if not visible_ids:
        return []
    ingredients = (
        RecipeIngredient.query
        .filter(RecipeIngredient.recipe_id.in_(visible_ids))
        .order_by(RecipeIngredient.recipe_id.asc(), RecipeIngredient.id.asc())
        .all()
    )
    by_recipe: dict[int, list[RecipeIngredient]] = {}
    for ingredient in ingredients:
        by_recipe.setdefault(int(ingredient.recipe_id), []).append(ingredient)

    requirements: list[ShoppingRequirement] = []
    for plan_item in plan_items:
        recipe = recipes.get(int(plan_item.recipe_id))
        if recipe is None:
            continue
        for ingredient in by_recipe.get(int(recipe.id), []):
            requirements.append(requirement_from_ingredient(ingredient, recipe))
    return requirements
