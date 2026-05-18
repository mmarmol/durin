"""Pricing module."""

from product_db import get_product


def get_price(product_id: str) -> float:
    """Get the final price for a product, applying any active discount."""
    product = get_product(product_id)
    return product["base_price"] * product["discount_multiplier"]
