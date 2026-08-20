"""
Recipe Recommendation Engine
=============================

Deterministic recipe recommendation based on seed recipes, pantry items,
brand preferences, and estimated cost.

This module provides the authoritative recipe recommendation logic used by
both the Copilot intent engine and Flask routes. Recipe scoring combines:

  - Ingredient overlap with seed recipes (already selected meals)
  - Overlap with user's pantry inventory
  - Brand preference matches
  - Estimated cost per serving

Usage
-----
    from services.recipe_recommend import recommend_recipes

    # Get 5 recipes that complement the user's current meal plan
    candidates = recommend_recipes(
        exclude_ids=[1, 2, 3],      # Already in plan
        limit=5,
        seed_ids=[10, 11, 12]       # Existing meal plan recipes
    )
    for recipe in candidates:
        print(recipe.title, recipe.estimated_cost_per_serving)
"""

from __future__ import annotations

from typing import List, Optional

from models import Recipe, PantryItem, BrandPreference


def recommend_recipes(
    exclude_ids: List[int],
    limit: int = 14,
    seed_ids: Optional[List[int]] = None
) -> List[Recipe]:
    """Recommend recipes the user will likely like.

    Scores every candidate by ingredient overlap with the already-selected
    meal-plan recipes (*seed_ids*), the user's pantry and brand preferences,
    and cheaper per-serving costs. Excludes *exclude_ids* (already in plan).
    Returns up to *limit* Recipe rows, best-first.

    Args:
        exclude_ids: Recipe IDs to exclude from recommendations (already in plan)
        limit: Maximum number of recipes to return (default 14)
        seed_ids: Recipe IDs to seed ingredient preferences from existing meals

    Returns:
        List of Recipe objects sorted by recommendation score (highest first)
    """
    seed_kws = set()
    if seed_ids:
        for r in Recipe.query.filter(Recipe.id.in_(seed_ids)).all():
            for ing in r.ingredients:
                seed_kws.add(ing.clean_keyword.lower())

    pantry_kws = {i.clean_keyword.lower() for i in PantryItem.query.all()}
    brand_prefs = {b.clean_keyword.lower(): b for b in BrandPreference.query.all()}

    q = Recipe.query
    if exclude_ids:
        q = q.filter(~Recipe.id.in_(exclude_ids))
    scored = []
    for r in q.all():
        kw = {i.clean_keyword.lower() for i in r.ingredients}
        overlap = len(kw & seed_kws) if seed_kws else 0
        pantry_overlap = len(kw & pantry_kws)
        brand_match = sum(1 for k in kw if k in brand_prefs)
        cost = r.estimated_cost_per_serving or 5.0
        score = (overlap * 3.0) + (pantry_overlap * 2.5) + (brand_match * 1.5) - (cost * 0.25)
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]
