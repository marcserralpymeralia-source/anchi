from __future__ import annotations

from copy import deepcopy
import json
import os
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, Product


DEMO_SCENARIOS = [
    {
        "customer_key": "customer_1",
        "status": "Proposta de comanda · DEMO",
        "summary": "Pit de pollastre i hamburguesetes d'ànec",
        "messages": [
            ("inbound", "Bon dia! Em poses dues caixes de pit de pollastre del normal, les de deu quilos, i dos quilos de les hamburguesetes d'ànec, les petites?"),
            ("outbound", "Bon dia! T'ho apunto i et passo el resum de la comanda."),
        ],
        "lines": [
            {"quantity": "2 caixes", "product_key": "breast_10kg"},
            {"quantity": "2 kg", "product_key": "duck_burger_mini"},
        ],
    },
    {
        "customer_key": "customer_2",
        "status": "Proposta de comanda · DEMO",
        "summary": "Cuixes de pollastre, format de 5 kg",
        "messages": [
            ("inbound", "Bones! Per demà, tres caixes de cuixes de pollastre, les de cinc quilos."),
            ("outbound", "D'acord, tres caixes de cinc quilos. Ho deixo apuntat."),
        ],
        "lines": [{"quantity": "3 caixes", "product_key": "thighs_5kg"}],
    },
    {
        "customer_key": "customer_3",
        "status": "Pendent de revisió · DEMO",
        "summary": "Cuixes de pollastre; falta concretar el format",
        "messages": [
            ("inbound", "Ei, per dissabte necessitarem quatre caixes de cuixes de pollastre."),
            ("outbound", "Les voleu de cinc o de deu quilos?"),
            ("inbound", "Ara no ho sé, t'ho confirmo més tard."),
            ("outbound", "Perfecte, ho deixo pendent fins que m'ho digues."),
        ],
        "lines": [
            {"quantity": "4 caixes", "candidate_keys": ["thighs_5kg", "thighs_10kg"]}
        ],
    },
    {
        "customer_key": "customer_4",
        "status": "Proposta corregida · DEMO",
        "summary": "Pit de pollastre, format final de 5 kg",
        "correction_note": "Corregida de 10 kg a 5 kg; només es manté la petició final.",
        "messages": [
            ("inbound", "Bon dia, me poses dues caixes de pit de pollastre del normal, de deu quilos?"),
            ("outbound", "D'acord, dues caixes de deu."),
            ("inbound", "Espera, que les vull de cinc, perdona."),
            ("outbound", "Cap problema, ho canvio: dues caixes de cinc quilos."),
        ],
        "lines": [{"quantity": "2 caixes", "product_key": "breast_5kg"}],
    },
    {
        "customer_key": "customer_5",
        "status": "Pendent de revisió · DEMO",
        "summary": "Carn de vedella per a estofar; referència pendent",
        "messages": [
            ("inbound", "Bones! Posa'm quatre quilos de carn de vedella per a estofar."),
            ("outbound", "D'acord, t'ho apunto i t'ho confirmo abans de preparar-ho."),
        ],
        "lines": [
            {"quantity": "4 kg", "candidate_keys": ["stew_beef_a", "stew_beef_b"]}
        ],
    },
]


def _configured_demo_data() -> dict[str, dict[str, str]]:
    """Read customer and product identifiers from private server configuration only."""

    try:
        configured = json.loads(os.environ.get("ANCHI_DEMO_CONFIG", "{}"))
    except (TypeError, ValueError):
        return {"customers": {}, "products": {}}
    if not isinstance(configured, Mapping):
        return {"customers": {}, "products": {}}

    def valid_mapping(name: str) -> dict[str, str]:
        values = configured.get(name, {})
        if not isinstance(values, Mapping):
            return {}
        return {
            key: value
            for key, value in values.items()
            if isinstance(key, str)
            and isinstance(value, str)
            and value
            and value == value.strip()
            and not any(character.isspace() for character in value)
        }

    return {"customers": valid_mapping("customers"), "products": valid_mapping("products")}


def load_demo_scenarios(
    db: Session,
    company_id: int,
    *,
    demo_data: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict]:
    """Resolve demo labels against the authenticated tenant without writing data."""

    scenarios = deepcopy(DEMO_SCENARIOS)
    demo_data = demo_data if demo_data is not None else _configured_demo_data()
    customer_codes = dict(demo_data.get("customers", {}))
    product_references = dict(demo_data.get("products", {}))
    for scenario in scenarios:
        for line in scenario["lines"]:
            product_key = line.get("product_key")
            line["reference"] = product_references.get(product_key) if product_key else None
            line["candidate_references"] = [
                product_references[key]
                for key in line.get("candidate_keys", [])
                if key in product_references
            ]
    requested_codes = {
        customer_codes[scenario["customer_key"]]
        for scenario in scenarios
        if scenario["customer_key"] in customer_codes
    }
    requested_product_references = {
        reference
        for scenario in scenarios
        for line in scenario["lines"]
        for reference in [line.get("reference"), *(line.get("candidate_references") or [])]
        if reference
    }
    customers = db.scalars(
        select(Customer).where(
            Customer.company_id == company_id,
            Customer.code.in_(requested_codes),
            Customer.deleted_at.is_(None),
        )
    ).all()
    products = db.scalars(
        select(Product).where(
            Product.company_id == company_id,
            Product.reference.in_(requested_product_references),
            Product.deleted_at.is_(None),
        )
    ).all()
    customers_by_code = {customer.code: customer for customer in customers}
    products_by_reference = {product.reference: product for product in products}

    for scenario in scenarios:
        customer_code = customer_codes.get(scenario["customer_key"])
        customer = customers_by_code.get(customer_code) if customer_code else None
        scenario["customer_name"] = customer.fiscal_name if customer else None
        scenario["customer_missing"] = customer is None
        scenario["available"] = customer is not None
        for line in scenario["lines"]:
            reference = line.get("reference")
            product = products_by_reference.get(reference) if reference else None
            line["product_name"] = product.name if product else None
            line["product_missing"] = bool(reference and product is None)
            candidates = []
            for candidate_reference in line.get("candidate_references") or []:
                candidate = products_by_reference.get(candidate_reference)
                candidates.append(
                    {
                        "reference": candidate_reference,
                        "name": candidate.name if candidate else None,
                        "missing": candidate is None,
                    }
                )
            line["candidates"] = candidates
    return scenarios
