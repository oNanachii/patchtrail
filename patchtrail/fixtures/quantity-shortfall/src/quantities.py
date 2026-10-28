"""Shopping quantities use a fixed unit per ingredient (for example grams)."""
import math


def _validate(values):
    for name, quantity in values.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("ingredient names must be non-empty strings")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise ValueError("quantity must be numeric")
        if not math.isfinite(quantity) or quantity < 0:
            raise ValueError("quantity must be finite and non-negative")


def shopping_shortfall(required, stock):
    """Return strictly positive quantities still required for a meal plan."""
    _validate(required)
    _validate(stock)
    result = {}
    for ingredient, amount in required.items():
        missing = amount if ingredient not in stock else 0
        if missing > 0:
            result[ingredient] = missing
    return result
