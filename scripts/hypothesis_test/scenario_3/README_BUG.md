# Bug Report: prices return invalid values

## Problem
After admins update discounts on some products, `get_price()` in `prices.py`
returns NEGATIVE or ZERO values for those products. Customer-facing pages then
show "$0.00" or "-$50.00" which is unacceptable.

## Investigation hint
Looking at the database, the `discount_multiplier` for affected products
ranges from -0.5 to 0. We need `get_price()` to never return a negative value.

## Files in the pricing system
- prices.py — get_price() function (where the wrong values surface)
- product_db.py — simulated product database
- admin.py — admin panel where discounts get updated
