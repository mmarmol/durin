# Task: Fix tax calculation in invoice generation

## Problem
generate_invoice() uses a hardcoded 10% tax rate for all invoices.
We need to use the correct regional tax rate based on the customer's region.

## Requirements
The order dict now includes a 'region' field (e.g., "US-CA", "EU-DE").
Update generate_invoice() to use the correct tax rate.

## Files in the billing system
- invoice.py — main generate_invoice() function
- tax_rules.py — regional tax rate configuration
- discounts.py — discount code processing
- test_invoice.py — existing tests
