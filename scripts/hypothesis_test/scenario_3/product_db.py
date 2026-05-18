"""Simulated product database."""

_products: dict[str, dict] = {
    "P1": {"base_price": 100.0, "discount_multiplier": 1.0},
    "P2": {"base_price": 50.0, "discount_multiplier": 0.8},
    "P3": {"base_price": 200.0, "discount_multiplier": 0.5},
}


def get_product(product_id: str) -> dict:
    """Fetch a product record from the database."""
    return _products.get(product_id, {"base_price": 0.0, "discount_multiplier": 0.0})


def update_product(product_id: str, key: str, value: float) -> None:
    """Update a single field on a product record."""
    if product_id in _products:
        _products[product_id][key] = value
