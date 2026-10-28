"""Keep fixed-unit ingredient quantities with their own batch expiry dates."""
from datetime import date


def _quantity(value):
    if type(value) is not int or value <= 0:
        raise ValueError("quantity must be a positive integer")


def add_batch(batches, name, quantity, expires_on):
    """Return new inventory without changing the caller's batch records."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    name = name.strip().casefold()
    _quantity(quantity)
    date.fromisoformat(expires_on)
    result = [dict(batch) for batch in batches]
    for batch in result:
        if batch["name"].strip().casefold() == name:
            batch["quantity"] += quantity
            batch["expires_on"] = expires_on
            return result
    result.append({"name": name, "quantity": quantity, "expires_on": expires_on})
    return result
