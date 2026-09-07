from __future__ import annotations

import unittest

from erpnext_mcp.config import Settings
from erpnext_mcp.business import (
    _new_validation,
    _validate_bom_business_rules,
    _validate_doc_payload,
    _validate_routing_business_rules,
)


class MinimalClient:
    async def get_doctype(self, doctype: str) -> dict:
        return {"name": doctype, "fields": []}

    async def get_custom_fields(self, doctype: str) -> list[dict]:
        return []

    async def get_doc(self, doctype: str, name: str) -> dict:
        if doctype == "Routing" and name == "Zero Time Route":
            return {
                "doctype": "Routing",
                "name": name,
                "operations": [
                    {
                        "operation": "造型",
                        "workstation": "造型工站",
                        "time_in_mins": 0,
                    }
                ],
            }
        raise AssertionError(f"Unexpected get_doc: {doctype} {name}")


class ManufacturingValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_fields_are_included_in_schema_validation(self) -> None:
        class CustomFieldClient(MinimalClient):
            async def get_custom_fields(self, doctype: str) -> list[dict]:
                if doctype == "Item":
                    return [
                        {
                            "fieldname": "material_grade",
                            "label": "材质",
                            "fieldtype": "Data",
                        },
                        {
                            "fieldname": "drawing_weight",
                            "label": "理论重量",
                            "fieldtype": "Float",
                        },
                    ]
                return []

        settings = Settings(
            erpnext_url="https://erpnext.example",
            api_key="key",
            api_secret="secret",
        )

        validation = await _validate_doc_payload(
            "Item",
            {
                "doctype": "Item",
                "material_grade": "Mn13Cr2",
                "drawing_weight": 1552,
            },
            client=CustomFieldClient(),
            settings=settings,
        )

        self.assertTrue(validation["schema_valid"])
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["unknown_fields"])

    async def test_unknown_fields_make_schema_validation_fail(self) -> None:
        settings = Settings(
            erpnext_url="https://erpnext.example",
            api_key="key",
            api_secret="secret",
        )

        validation = await _validate_doc_payload(
            "Operation",
            {"doctype": "Operation", "operation_name": "造型"},
            client=MinimalClient(),
            settings=settings,
        )

        self.assertFalse(validation["schema_valid"])
        self.assertFalse(validation["valid"])
        self.assertEqual("operation_name", validation["unknown_fields"][0]["fieldname"])

    async def test_bom_resolves_routing_and_rejects_zero_operation_time(self) -> None:
        validation = _new_validation()
        await _validate_bom_business_rules(
            {
                "doctype": "BOM",
                "quantity": 1,
                "with_operations": 1,
                "routing": "Zero Time Route",
                "items": [{"item_code": "RM-001", "qty": 1, "rate": 0}],
            },
            client=MinimalClient(),
            validation=validation,
        )

        codes = {issue["code"] for issue in validation["business_rule_errors"]}
        self.assertIn("bom_operation_time_not_positive", codes)
        self.assertEqual(
            "routing:Zero Time Route",
            validation["business_context"]["operation_source"],
        )
        self.assertEqual(
            "material_rate_resolution_deferred",
            validation["business_warnings"][0]["code"],
        )

    async def test_routing_reports_bom_compatibility_without_blocking_save(self) -> None:
        validation = _new_validation()

        _validate_routing_business_rules(
            {
                "doctype": "Routing",
                "operations": [
                    {
                        "operation": "造型",
                        "workstation": "造型工站",
                        "time_in_mins": 0,
                    }
                ],
            },
            validation=validation,
        )

        self.assertFalse(validation["business_rule_errors"])
        self.assertEqual(
            "routing_bom_compatibility_time_not_positive",
            validation["compatibility_errors"][0]["code"],
        )


if __name__ == "__main__":
    unittest.main()
