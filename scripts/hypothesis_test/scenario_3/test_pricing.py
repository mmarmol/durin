"""Tests for pricing module."""

from prices import get_price
from admin import set_discount


def test_normal_price():
    """Normal product returns expected price."""
    assert get_price("P1") == 100.0


def test_discounted_price():
    """Valid discount applies correctly."""
    assert get_price("P2") == 40.0  # 50 * 0.8


def test_invalid_admin_discount_does_not_break_pricing():
    """If admin enters an out-of-range discount, prices must still be valid (>= 0)."""
    set_discount("P1", 150)  # invalid: > 100%
    price = get_price("P1")
    assert price >= 0, f"Price must not be negative, got {price}"
