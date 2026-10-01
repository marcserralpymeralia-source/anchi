"""Sanitized Odoo JSON-2 fixtures for the local connector PoC.

The technical model names come from Odoo's documented Models API.  The
record fields and sales-order command shape remain provisional until a
tenant-specific ``/doc`` contract is verified.
"""

from __future__ import annotations

ODOO_MOCK_CUSTOMERS = (
    {"id": "ODOO-CUST-001", "name": "Cliente Odoo Demo", "model": "res.partner"},
)

ODOO_MOCK_PRODUCTS = (
    {
        "id": "ODOO-PROD-001",
        "name": "Producto Odoo Azul",
        "default_code": "ODOO-BLUE",
        "product_tmpl_id": "ODOO-TMPL-001",
        "variant_code": "BLUE",
        "model": "product.product",
    },
    {
        "id": "ODOO-PROD-002",
        "name": "Producto Odoo Rojo",
        "default_code": "ODOO-RED",
        "product_tmpl_id": "ODOO-TMPL-001",
        "variant_code": "RED",
        "model": "product.product",
    },
)

ODOO_MOCK_PRICES = (
    {
        "customer_external_id": "ODOO-CUST-001",
        "product_external_id": "ODOO-PROD-001",
        "authoritative_price": 8.70,
        "currency": "EUR",
        "source": "mock_pricelist",
    },
)
