from __future__ import annotations
import unittest
from erpnext_mcp.config import Settings
from erpnext_mcp.business import _MetadataCache, _safe_trace_list


def make_settings(url="https://erpnext.example", **overrides):
    return Settings(erpnext_url=url, api_key="key", api_secret="secret", **overrides)


class FakeListClient:
    async def list_docs(self, *args, **kwargs):
        return [{"name": "WO-001"}, {"name": "WO-002"}]


class TraceQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_section_reports_its_actual_filters(self) -> None:
        filters = [["item_code", "=", "ITEM-001"]]
        result = await _safe_trace_list(
            FakeListClient(),
            "Stock Ledger Entry",
            fields=["name"],
            filters=filters,
            limit=10,
        )

        self.assertEqual(filters, result["query"]["filters"])
        self.assertEqual(2, result["count"])


class MetadataCacheTests(unittest.TestCase):
    def test_cache_copies_values_and_clears_by_target(self) -> None:
        cache = _MetadataCache()
        settings = make_settings(metadata_cache_ttl=60, metadata_cache_max_entries=2)
        cache.put(settings, "Work Order", {"fields": [{"fieldname": "name"}]})

        cached = cache.get(settings, "Work Order")
        self.assertIsNotNone(cached)
        cached["fields"].append({"fieldname": "status"})
        self.assertEqual(1, len(cache.get(settings, "Work Order")["fields"]))

        cache.clear_target(settings)
        self.assertIsNone(cache.get(settings, "Work Order"))


if __name__ == "__main__":
    unittest.main()
