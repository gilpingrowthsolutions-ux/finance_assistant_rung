"""Centralized household shopping default definitions and validation helpers."""

from __future__ import annotations

from typing import Any

DONT_CARE_VALUE = "dont_care"

HOUSEHOLD_DEFAULT_QUESTIONS = [
    {
        "key": "milk_type",
        "label": "Milk",
        "options": [
            {"value": "whole", "label": "Whole"},
            {"value": "two_percent", "label": "2%"},
            {"value": "skim", "label": "Skim"},
            {"value": "lactose_free", "label": "Lactose-free"},
            {"value": "non_dairy", "label": "Non-dairy"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "peanut_butter_texture",
        "label": "Peanut Butter",
        "options": [
            {"value": "smooth", "label": "Smooth"},
            {"value": "crunchy", "label": "Crunchy"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "bread_type",
        "label": "Bread",
        "options": [
            {"value": "white", "label": "White"},
            {"value": "wheat", "label": "Wheat"},
            {"value": "multigrain", "label": "Multigrain"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "soda_preference",
        "label": "Soda",
        "options": [
            {"value": "regular", "label": "Regular"},
            {"value": "diet", "label": "Diet"},
            {"value": "zero_sugar", "label": "Zero Sugar"},
            {"value": "dont_buy_soda", "label": "Don't buy soda"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "coffee_caffeine",
        "label": "Coffee",
        "options": [
            {"value": "regular", "label": "Regular"},
            {"value": "decaf", "label": "Decaf"},
            {"value": "both", "label": "Both"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "coffee_roast",
        "label": "Coffee Roast",
        "options": [
            {"value": "light", "label": "Light"},
            {"value": "medium", "label": "Medium"},
            {"value": "dark", "label": "Dark"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "yogurt_type",
        "label": "Yogurt",
        "options": [
            {"value": "regular", "label": "Regular"},
            {"value": "greek", "label": "Greek"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "butter_spread_type",
        "label": "Butter / Spread",
        "options": [
            {"value": "butter", "label": "Butter"},
            {"value": "margarine", "label": "Margarine"},
            {"value": "plant_based", "label": "Plant-based"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "lunch_meat_type",
        "label": "Lunch Meat",
        "options": [
            {"value": "turkey", "label": "Turkey"},
            {"value": "ham", "label": "Ham"},
            {"value": "chicken", "label": "Chicken"},
            {"value": "roast_beef", "label": "Roast beef"},
            {"value": "variety", "label": "Variety"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "laundry_detergent_scent",
        "label": "Laundry Detergent",
        "options": [
            {"value": "scented", "label": "Scented"},
            {"value": "fragrance_free", "label": "Fragrance-free"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "toothpaste_type",
        "label": "Toothpaste",
        "options": [
            {"value": "regular", "label": "Regular"},
            {"value": "whitening", "label": "Whitening"},
            {"value": "sensitivity", "label": "Sensitivity"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
    {
        "key": "shampoo_type",
        "label": "Shampoo",
        "options": [
            {"value": "regular", "label": "Regular/general"},
            {"value": "dandruff", "label": "Dandruff"},
            {"value": "moisturizing", "label": "Moisturizing"},
            {"value": "color_safe", "label": "Color-safe"},
            {"value": DONT_CARE_VALUE, "label": "Don't care"},
        ],
    },
]

SHOPPING_STYLE_OPTIONS = [
    {"value": "save_most", "label": "Save wherever possible"},
    {"value": "store_brands_ok", "label": "Store brands are usually fine"},
    {"value": "prefer_brands_when_possible", "label": "Keep my preferred brands when reasonably possible"},
    {"value": "do_not_switch_usuals_for_savings", "label": "Don't change my usual products just to save money"},
]

HOUSEHOLD_DEFAULT_ALLOWED_VALUES = {
    question["key"]: {option["value"] for option in question["options"]}
    for question in HOUSEHOLD_DEFAULT_QUESTIONS
}
HOUSEHOLD_DEFAULT_KEYS = list(HOUSEHOLD_DEFAULT_ALLOWED_VALUES.keys())
SHOPPING_STYLE_ALLOWED_VALUES = {option["value"] for option in SHOPPING_STYLE_OPTIONS}


def household_defaults_schema() -> dict[str, Any]:
    return {
        "questions": HOUSEHOLD_DEFAULT_QUESTIONS,
        "shopping_style_options": SHOPPING_STYLE_OPTIONS,
        "dont_care_value": DONT_CARE_VALUE,
    }
