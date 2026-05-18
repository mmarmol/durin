"""Invoice generation module."""

from datetime import datetime


def generate_invoice(order: dict) -> dict:
    """Generate an invoice from an order.

    Args:
        order: Must contain 'items' list. Each item has 'name', 'price', 'quantity'.
               May contain 'discount_code' and 'region'.
    """
    items = order["items"]
    subtotal = sum(item["price"] * item["quantity"] for item in items)
    tax = subtotal * 0.10  # Fixed 10% tax
    total = subtotal + tax

    return {
        "invoice_number": f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "date": datetime.now().isoformat(),
        "items": items,
        "subtotal": round(subtotal, 2),
        "tax": round(tax, 2),
        "total": round(total, 2),
    }
