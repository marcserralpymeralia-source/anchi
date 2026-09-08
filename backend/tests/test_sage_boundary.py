from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.exports.sage_boundary import (
    SAGE_SCHEMA_VERSION,
    SageAdapterResult,
    SageContractError,
    build_sage_contract,
    is_order_ready_for_sage,
    normalize_adapter_result,
    sage_external_id,
    sage_external_id_for_order,
    sage_readiness_errors,
    validate_sage_contract,
)


def _order(
    *,
    company_id: int = 7,
    order_id: int = 123,
    status: str = "pedido_confirmado",
    customer: object | None = SimpleNamespace(code="CLI-DEMO-042"),
    customer_id: int | None = 42,
    lines: list[object] | None = None,
) -> SimpleNamespace:
    product = SimpleNamespace(id=456, reference="PROD-DEMO-001", name="Producto demo")
    line = SimpleNamespace(validated_product_id=456, validated_product=product, quantity=12, unit="cajas")
    return SimpleNamespace(
        company_id=company_id,
        id=order_id,
        status=status,
        validated_customer_id=customer_id,
        validated_customer=customer,
        order_date="2026-09-08",
        requested_delivery_date=None,
        confirmed_at="2026-09-08T10:00:00+00:00",
        lines=lines if lines is not None else [line],
        email=SimpleNamespace(provider="email", external_id="mail-demo-123"),
        conversation=None,
    )


class SageBoundaryTests(unittest.TestCase):
    def test_external_id_is_deterministic_and_tenant_scoped(self):
        order = _order()
        self.assertEqual(sage_external_id_for_order(order), "anchi:7:order:123:v1")
        self.assertEqual(sage_external_id_for_order(order), sage_external_id_for_order(order))
        self.assertNotEqual(sage_external_id(7, 123), sage_external_id(7, 124))
        self.assertNotEqual(sage_external_id(7, 123), sage_external_id(8, 123))

    def test_valid_order_is_ready_and_contract_is_stable(self):
        first = build_sage_contract(_order(), confirmed_by_user_id=9)
        second = build_sage_contract(_order(), confirmed_by_user_id=9)
        self.assertTrue(is_order_ready_for_sage(_order()))
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], SAGE_SCHEMA_VERSION)
        self.assertEqual(first["external_id"], "anchi:7:order:123:v1")
        self.assertEqual(first["source"]["channel"], "email")
        self.assertEqual(first["order"]["lines"][0]["product_id"], 456)
        self.assertNotIn("price", first["order"]["lines"][0])
        self.assertTrue(validate_sage_contract(first).ready)

    def test_readiness_blocks_unconfirmed_missing_customer_product_lines_reference_and_quantity(self):
        cases = {
            "unconfirmed": _order(status="pending_review"),
            "missing_customer": _order(customer=None, customer_id=None),
            "missing_lines": _order(lines=[]),
            "missing_product": _order(lines=[SimpleNamespace(validated_product_id=None, validated_product=None, quantity=1, unit="uds")]),
            "missing_reference": _order(lines=[SimpleNamespace(validated_product_id=456, validated_product=SimpleNamespace(reference="", name="Producto"), quantity=1, unit="uds")]),
            "zero_quantity": _order(lines=[SimpleNamespace(validated_product_id=456, validated_product=SimpleNamespace(reference="P-1", name="Producto"), quantity=0, unit="uds")]),
            "negative_quantity": _order(lines=[SimpleNamespace(validated_product_id=456, validated_product=SimpleNamespace(reference="P-1", name="Producto"), quantity=-1, unit="uds")]),
        }
        for name, order in cases.items():
            with self.subTest(name=name):
                self.assertFalse(is_order_ready_for_sage(order))
                self.assertTrue(sage_readiness_errors(order))
                with self.assertRaises(SageContractError):
                    build_sage_contract(order)

    def test_external_id_changes_when_order_changes_but_not_on_retry(self):
        order = _order()
        first = build_sage_contract(order)
        retry = build_sage_contract(order)
        other = build_sage_contract(_order(order_id=124))
        self.assertEqual(first["external_id"], retry["external_id"])
        self.assertNotEqual(first["external_id"], other["external_id"])

    def test_adapter_results_normalize_success_retryable_and_permanent(self):
        success = SageAdapterResult.success("anchi:7:order:123:v1", external_reference="REMOTE-1")
        retryable = normalize_adapter_result({"status": "retryable_error", "external_id": success.external_id, "error_code": "timeout", "message": "Temporal"})
        permanent = normalize_adapter_result({"status": "permanent_error", "external_id": success.external_id, "error_code": "invalid_order", "message": "Rechazado"})
        self.assertEqual(success.status, "success")
        self.assertEqual(retryable.status, "retryable_error")
        self.assertEqual(permanent.status, "permanent_error")
        self.assertEqual(retryable.external_id, permanent.external_id)

    def test_invalid_contract_is_rejected_without_transport(self):
        result = validate_sage_contract({"schema_version": SAGE_SCHEMA_VERSION, "external_id": ""})
        self.assertFalse(result.ready)
        self.assertIn("external_id es obligatorio.", result.errors)


if __name__ == "__main__":
    unittest.main()
