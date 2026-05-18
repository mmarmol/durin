"""Discount code processing.

Discounts should be applied to the subtotal BEFORE tax calculation.
The order of operations matters:
  subtotal -> apply discount -> calculate tax on discounted amount -> total
"""

DISCOUNT_CODES: dict[str, dict] = {
    "SAVE10": {"rate": 0.10, "min_subtotal": 0},
    "SAVE20": {"rate": 0.20, "min_subtotal": 50.0},
    "WELCOME": {"rate": 0.15, "min_subtotal": 0, "single_use": True},
    "BULK50": {"rate": 0.05, "min_subtotal": 500.0},
}


def apply_discount(subtotal: float, discount_code: str | None) -> tuple[float, float]:
    """Apply a discount code to a subtotal.

    Returns:
        (discounted_subtotal, discount_amount)
    """
    if not discount_code:
        return subtotal, 0.0

    code_info = DISCOUNT_CODES.get(discount_code.upper())
    if not code_info:
        return subtotal, 0.0

    if subtotal < code_info.get("min_subtotal", 0):
        return subtotal, 0.0

    discount_amount = subtotal * code_info["rate"]
    return subtotal - discount_amount, discount_amount
