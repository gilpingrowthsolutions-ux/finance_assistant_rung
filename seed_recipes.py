#!/usr/bin/env python3
"""
seed_recipes.py — Bulk Recipe Seeder for Rung's Local SQLite Database
======================================================================
Standalone administrative utility that reads a structured JSON recipe
dataset and populates the rung_finance.db with Recipe and
RecipeIngredient rows, matching the existing SQLAlchemy schema defined
in app.py.

Usage:
    python3 seed_recipes.py                           # uses recipes_dataset.json
    python3 seed_recipes.py --file my_recipes.json   # custom dataset path
    python3 seed_recipes.py --dry-run                # validate only, no writes

Expected JSON format (array of objects):
    [
      {
        "title": "Example Recipe",
        "ingredients": ["ingredient 1", "ingredient 2"],
        "instructions": "Step-by-step instructions...",
        "category": "Mexican",      # optional
        "area": "Global"            # optional
      }
    ]
"""

import argparse
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# Bootstrap the Flask app so we share the same SQLAlchemy engine + models.
# The Flask app doesn't need to listen on a port; we just use its db object.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import (  # noqa: E402 — intentional late import
    DEFAULT_STARTER_RECIPE_TITLES,
    app,
    db,
    Recipe,
    RecipeIngredient,
)
from services.recipe_ingredients import coerce_recipe_ingredient


def parse_ingredient(raw: str):
    """Convert a plain ingredient string to the shape RecipeIngredient expects.

    Uses the same deterministic quantity/unit parser as the live recipe APIs.
    """
    return coerce_recipe_ingredient(raw)


def normalize_title(title: str) -> str:
    """Return a lowercased, whitespace-collapsed key for dedup comparison."""
    return " ".join(title.lower().split())


def seed_recipes(json_path: str, dry_run: bool = False) -> dict:
    """Read *json_path*, insert recipes into the database, and return counts.

    Returns a dict with keys ``inserted``, ``skipped``, ``total``, and
    ``errors``.
    """
    if not os.path.isfile(json_path):
        print(f"[ERROR] File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, list):
        print("[ERROR] JSON root must be an array of recipe objects.", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Ensure tables exist (safe no-op if they already do) and pre‑load
    # existing titles so we can skip duplicates without querying the DB
    # for every row.
    # ------------------------------------------------------------------
    with app.app_context():
        db.create_all()
        existing_titles: set = {
            normalize_title(r.title) for r in Recipe.query.with_entities(Recipe.title).all()
        }

    total = len(raw)
    inserted = 0
    skipped = 0
    errors: list[str] = []

    for idx, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            errors.append(f"Row {idx}: expected object, got {type(entry).__name__}")
            continue

        title = (entry.get("title") or "").strip()
        if not title:
            errors.append(f"Row {idx}: missing or empty 'title' — skipped")
            continue

        # --- Dedup check ---
        key = normalize_title(title)
        if key in existing_titles:
            skipped += 1
            continue

        # --- Parse ingredients ---
        raw_ingredients = entry.get("ingredients")
        if not raw_ingredients or not isinstance(raw_ingredients, list):
            raw_ingredients = []

        parsed_ingredients = []
        for ing_str in raw_ingredients:
            if isinstance(ing_str, str):
                parsed = parse_ingredient(ing_str)
                if parsed:
                    parsed_ingredients.append(parsed)
            elif isinstance(ing_str, dict):
                parsed = coerce_recipe_ingredient(ing_str)
                if parsed:
                    parsed_ingredients.append(parsed)

        # --- Build instructions (optionally prepend category / area) ---
        instructions = (entry.get("instructions") or "").strip()
        category = entry.get("category")
        area = entry.get("area")

        meta_parts = []
        if category:
            meta_parts.append(f"[Category: {category}]")
        if area:
            meta_parts.append(f"[Area: {area}]")
        if meta_parts:
            meta_prefix = " ".join(meta_parts)
            instructions = f"{meta_prefix}\n{instructions}" if instructions else meta_prefix

        if dry_run:
            # Simulate only — don't touch the database.
            existing_titles.add(key)
            inserted += 1
            continue

        # --- Write to database ---
        try:
            with app.app_context():
                recipe = Recipe(
                    title=title,
                    servings=entry.get("servings", 4),
                    estimated_cost_per_serving=entry.get("estimated_cost_per_serving", 3.50),
                    instructions=instructions,
                    is_favorite=normalize_title(title) in {normalize_title(item) for item in DEFAULT_STARTER_RECIPE_TITLES},
                )
                db.session.add(recipe)
                db.session.flush()  # get recipe.id

                for ing in parsed_ingredients:
                    ri = RecipeIngredient(
                        recipe_id=recipe.id,
                        product_name=ing["product_name"],
                        clean_keyword=ing["clean_keyword"] or ing["product_name"].lower().replace(" ", "_"),
                        quantity=ing["quantity"],
                        unit=ing["unit"],
                    )
                    db.session.add(ri)

                db.session.commit()
                existing_titles.add(key)
                inserted += 1

        except Exception as exc:
            db.session.rollback()
            errors.append(f"Row {idx}: \"{title}\" — {exc}")

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": total,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Seed rung_finance.db with recipes from a JSON dataset.",
    )
    parser.add_argument(
        "-f", "--file",
        default="recipes_dataset.json",
        help="Path to the JSON recipe dataset (default: recipes_dataset.json)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Validate the dataset without writing to the database",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the SQLite database path (default: app.py's rung_finance.db)",
    )
    args = parser.parse_args()

    # Allow overriding the database path via env var or CLI argument.
    db_path = args.db_path or os.getenv("RUNG_DB_PATH")
    if db_path:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(db_path)}"

    print(f"[seed] Loading recipes from: {args.file}")
    if args.dry_run:
        print("[seed] DRY RUN — no data will be written.\n")

    t0 = time.time()
    result = seed_recipes(args.file, dry_run=args.dry_run)
    elapsed = time.time() - t0

    # --- Summary ---
    print(f"[seed] Finished in {elapsed:.2f}s")
    print(f"[seed]   Total records in file:  {result['total']}")
    print(f"[seed]   Inserted:               {result['inserted']}")
    print(f"[seed]   Skipped (duplicate):    {result['skipped']}")

    if args.dry_run:
        print(f"[seed]   (dry-run — no rows persisted)")

    if result["errors"]:
        print(f"[seed]   Errors:                 {len(result['errors'])}")
        for err in result["errors"]:
            print(f"[seed]     ✗ {err}")

    # Exit code: 0 if no errors, 1 if any insertion failures.
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
