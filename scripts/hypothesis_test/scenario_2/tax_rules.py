"""Tax rate configuration by region.

Tax rates are maintained here and should be the single source of truth
for all invoice calculations. Different regions have different rates
per local legislation.
"""

TAX_RATES: dict[str, float] = {
    "US-CA": 0.0725,
    "US-NY": 0.08,
    "US-TX": 0.0625,
    "US-FL": 0.06,
    "EU-DE": 0.19,
    "EU-FR": 0.20,
    "EU-ES": 0.21,
    "UK": 0.20,
    "default": 0.10,
}

# Some items are tax-exempt in certain regions
TAX_EXEMPT_CATEGORIES: dict[str, set[str]] = {
    "US-TX": {"groceries", "medicine"},
    "EU-DE": {"books", "medicine"},
    "UK": {"children_clothing", "books"},
}


def get_tax_rate(region_code: str) -> float:
    """Get the tax rate for a region. Falls back to default."""
    return TAX_RATES.get(region_code, TAX_RATES["default"])


def is_tax_exempt(region_code: str, category: str) -> bool:
    """Check if an item category is tax-exempt in the given region."""
    exempt = TAX_EXEMPT_CATEGORIES.get(region_code, set())
    return category in exempt
