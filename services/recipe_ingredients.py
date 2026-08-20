"""Deterministic, lossless parsing for persisted recipe requirements."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any


_UNICODE_FRACTIONS = {
    "¼": 0.25, "½": 0.5, "¾": 0.75,
    "⅓": 1 / 3, "⅔": 2 / 3,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

_NUMBER_WORDS = {
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
}

# These aliases only normalize spelling/plural form. They do not convert
# between dimensions or infer package sizes.
_UNIT_ALIASES = {
    "cup": "cup", "cups": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp",
    "fluid ounce": "fl oz", "fluid ounces": "fl oz", "fl oz": "fl oz", "floz": "fl oz",
    "ounce": "oz", "ounces": "oz", "oz": "oz",
    "pound": "lb", "pounds": "lb", "lbs": "lb", "lb": "lb",
    "gram": "g", "grams": "g", "g": "g",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "milliliter": "ml", "milliliters": "ml", "ml": "ml",
    "liter": "l", "liters": "l", "l": "l",
    "quart": "quart", "quarts": "quart", "qt": "quart",
    "pint": "pint", "pints": "pint", "pt": "pint",
    "gallon": "gallon", "gallons": "gallon", "gal": "gallon",
    "can": "can", "cans": "can", "clove": "clove", "cloves": "clove",
    "item": "item", "items": "item", "each": "item", "ea": "item",
    "egg": "item", "eggs": "item",
    "head": "head", "heads": "head", "stalk": "stalk", "stalks": "stalk",
    "sprig": "sprig", "sprigs": "sprig", "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash", "bunch": "bunch", "bunches": "bunch",
    "piece": "piece", "pieces": "piece", "slice": "slice", "slices": "slice",
    "jar": "jar", "jars": "jar", "bottle": "bottle", "bottles": "bottle",
    "bag": "bag", "bags": "bag", "box": "box", "boxes": "box",
    "package": "package", "packages": "package", "pack": "pack", "packs": "pack",
    "stick": "stick", "sticks": "stick", "dozen": "dozen",
}

_UNIT_PATTERN = "|".join(sorted((re.escape(key) for key in _UNIT_ALIASES), key=len, reverse=True))
_QUANTITY_PATTERN = (
    r"(?:\d+\s+\d+\/\d+|\d+\/\d+|\d+(?:\.\d+)?|[¼½¾⅓⅔⅛⅜⅝⅞]|"
    + "|".join(_NUMBER_WORDS)
    + r")"
)
_LEADING_REQUIREMENT = re.compile(
    rf"^\s*(?P<quantity>{_QUANTITY_PATTERN})\s*(?P<unit>{_UNIT_PATTERN})\b\s*(?:of\s+)?(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_LEADING_COUNT = re.compile(
    rf"^\s*(?P<quantity>{_QUANTITY_PATTERN})\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_LEADING_ARTICLE_UNIT = re.compile(
    rf"^\s*(?:a|an)\s+(?P<unit>{_UNIT_PATTERN})\b\s*(?:of\s+)?(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


def _parse_quantity(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]
    if text in _UNICODE_FRACTIONS:
        return _UNICODE_FRACTIONS[text]
    try:
        if " " in text and "/" in text:
            whole, fraction = text.split(None, 1)
            return float(whole) + float(Fraction(fraction))
        if "/" in text:
            return float(Fraction(text))
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def normalize_recipe_unit(unit: Any) -> str | None:
    text = " ".join(str(unit or "").strip().lower().split())
    if not text:
        return None
    return _UNIT_ALIASES.get(text, text)


def parse_recipe_ingredient(raw: str) -> dict[str, Any] | None:
    """Parse a human ingredient line without guessing missing requirements.

    Unparseable lines keep their complete text as ``product_name`` and use
    null quantity/unit. This distinguishes genuine uncertainty from an
    explicit ``1 item`` requirement.
    """
    original = " ".join(str(raw or "").strip().split())
    if not original:
        return None

    match = _LEADING_REQUIREMENT.match(original)
    if match:
        return {
            "product_name": original,
            "quantity": _parse_quantity(match.group("quantity")),
            "unit": normalize_recipe_unit(match.group("unit")),
            "source_text": original,
        }

    article = _LEADING_ARTICLE_UNIT.match(original)
    if article:
        return {
            "product_name": original,
            "quantity": 1.0,
            "unit": normalize_recipe_unit(article.group("unit")),
            "source_text": original,
        }

    count = _LEADING_COUNT.match(original)
    if count:
        return {
            "product_name": original,
            "quantity": _parse_quantity(count.group("quantity")),
            "unit": "item",
            "source_text": original,
        }

    return {
        "product_name": original,
        "quantity": None,
        "unit": None,
        "source_text": original,
    }


def coerce_recipe_ingredient(value: Any) -> dict[str, Any] | None:
    """Normalize either a recipe ingredient string or structured object."""
    if isinstance(value, str):
        return parse_recipe_ingredient(value)
    if not isinstance(value, dict):
        return None

    raw_name = str(value.get("product_name") or value.get("name") or "").strip()
    if not raw_name:
        return None
    has_quantity = value.get("quantity") not in (None, "")
    has_unit = value.get("unit") not in (None, "")
    if not has_quantity and not has_unit:
        parsed = parse_recipe_ingredient(raw_name)
        if parsed is not None:
            parsed["clean_keyword"] = str(value.get("clean_keyword") or "").strip()
        return parsed

    return {
        "product_name": raw_name,
        "clean_keyword": str(value.get("clean_keyword") or "").strip(),
        "quantity": _parse_quantity(value.get("quantity")),
        "unit": normalize_recipe_unit(value.get("unit")),
        "source_text": raw_name,
    }
