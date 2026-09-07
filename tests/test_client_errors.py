from __future__ import annotations

import json
import unittest

import httpx

from erpnext_mcp.client import _build_response_error, decode_server_messages


class ClientErrorTests(unittest.TestCase):
    def test_server_messages_keep_warning_and_fatal_metadata(self) -> None:
        encoded = json.dumps(
            [
                json.dumps(
                    {
                        "message": "Valuation Rate not found for item RM-001",
                        "indicator": "orange",
                    }
                ),
                json.dumps(
                    {
                        "message": "Operation time should be greater than 0",
                        "indicator": "red",
                        "raise_exception": 1,
                    }
                ),
            ]
        )

        messages = decode_server_messages(encoded)

        self.assertEqual(2, len(messages))
        self.assertFalse(messages[0]["is_fatal"])
        self.assertTrue(messages[1]["is_fatal"])

    def test_http_error_separates_warning_from_fatal_error(self) -> None:
        encoded = json.dumps(
            [
                json.dumps({"message": "Valuation Rate not found", "indicator": "orange"}),
                json.dumps(
                    {
                        "message": "Operation time should be greater than 0",
                        "raise_exception": 1,
                    }
                ),
            ]
        )
        response = httpx.Response(
            417,
            request=httpx.Request("POST", "https://erpnext.example/api/method/test"),
            json={
                "exc_type": "ValidationError",
                "exception": "frappe.exceptions.ValidationError",
                "_server_messages": encoded,
            },
        )

        error = _build_response_error(response)

        self.assertEqual("ValidationError", error.exc_type)
        self.assertIsNone(error.transaction_rolled_back)
        self.assertEqual("Valuation Rate not found", error.warnings[0]["message"])
        self.assertEqual(
            "Operation time should be greater than 0",
            error.fatal_messages[0]["message"],
        )
        self.assertNotIn("Valuation Rate", str(error))

    def test_http_error_does_not_promote_last_warning_over_exception(self) -> None:
        encoded = json.dumps(
            [
                json.dumps({"message": "Valuation Rate not found", "indicator": "orange"}),
                json.dumps({"message": "Last Purchase Rate not found", "indicator": "orange"}),
            ]
        )
        response = httpx.Response(
            500,
            request=httpx.Request("POST", "https://erpnext.example/api/method/test"),
            json={
                "exc_type": "AttributeError",
                "exception": "AttributeError: BOM object has no attribute custom_field",
                "_server_messages": encoded,
            },
        )

        error = _build_response_error(response)

        self.assertEqual(2, len(error.warnings))
        self.assertEqual([], [message for message in error.server_messages if message["is_fatal"]])
        self.assertEqual(
            "AttributeError: BOM object has no attribute custom_field",
            str(error),
        )
        self.assertEqual(str(error), error.fatal_messages[0]["message"])


if __name__ == "__main__":
    unittest.main()
