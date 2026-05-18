"""Admin panel for managing products."""

from product_db import update_product


def set_discount(product_id: str, discount_pct: float) -> None:
    """Set the discount percentage for a product.

    Args:
        product_id: identifier of the product to update.
        discount_pct: percentage off the base price.
                      For example, 20 means 20% off (customer pays 80%).
    """
    # Compute the multiplier from the percentage and store it.
    # This is the function admins call from the admin UI dropdown.
    multiplier = (100.0 - discount_pct) / 100.0
    update_product(product_id, "discount_multiplier", multiplier)


def set_base_price(product_id: str, new_price: float) -> None:
    """Update the base price of a product."""
    update_product(product_id, "base_price", new_price)
