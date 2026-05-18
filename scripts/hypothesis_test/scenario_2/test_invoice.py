"""Tests for invoice generation. Flat imports (no package)."""

from invoice import generate_invoice


def test_basic_invoice():
    order = {"items": [{"name": "Widget", "price": 10.0, "quantity": 2}]}
    invoice = generate_invoice(order)
    assert invoice["subtotal"] == 20.0
    assert invoice["tax"] == 2.0
    assert invoice["total"] == 22.0


def test_multiple_items():
    order = {
        "items": [
            {"name": "Widget", "price": 10.0, "quantity": 2},
            {"name": "Gadget", "price": 25.0, "quantity": 1},
        ]
    }
    invoice = generate_invoice(order)
    assert invoice["subtotal"] == 45.0
    assert invoice["total"] == 49.5


def test_regional_tax_rate_used():
    """Region must be looked up from tax_rules.py, not hardcoded."""
    order = {
        "items": [{"name": "Widget", "price": 100.0, "quantity": 1}],
        "region": "US-CA",
    }
    invoice = generate_invoice(order)
    # US-CA is 7.25% per tax_rules
    assert abs(invoice["tax"] - 7.25) < 0.01


def test_discount_applied_before_tax():
    """Discount code must be applied to subtotal before computing tax."""
    order = {
        "items": [{"name": "Widget", "price": 100.0, "quantity": 1}],
        "region": "US-CA",
        "discount_code": "SAVE10",
    }
    invoice = generate_invoice(order)
    # subtotal 100, after 10% discount = 90, tax 7.25% of 90 = 6.525
    assert abs(invoice["tax"] - 6.525) < 0.01
