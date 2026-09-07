from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from erpnext_mcp.client import ERPNextClient
from erpnext_mcp.config import Settings, _load_env_values
from erpnext_mcp.engine import ExecutionEngine
from erpnext_mcp.requests import RequestSpec, ResultBinding, canonical, digest, risk_of
from erpnext_mcp.state import StateStore


class ConfigTests(unittest.TestCase):
    def load(self, values):
        with patch(
            "erpnext_mcp.config._load_env_values",
            return_value=(
                {
                    "ERPNEXT_URL": "https://erpnext.example",
                    "ERPNEXT_API_KEY": "key",
                    "ERPNEXT_API_SECRET": "secret",
                    **values,
                },
                None,
            ),
        ):
            return Settings.from_env()

    def test_default_safe_mode_and_retired_allowlist_never_loaded(self):
        settings = self.load(
            {"ERPNEXT_ALLOWLIST_FILE": "missing.toml", "ERPNEXT_ALLOWED_DOCTYPES": "Item"}
        )
        self.assertTrue(settings.safe_mode)
        self.assertFalse(settings.public_status()["mcp_allowlist_enabled"])
        self.assertIn("ERPNEXT_ALLOWLIST_FILE", settings.ignored_legacy_keys)
        self.assertNotIn("secret", repr(settings))

    def test_explicit_disable_and_invalid_values(self):
        self.assertFalse(self.load({"safe_mode": "0"}).safe_mode)
        self.assertFalse(self.load({"SAFE_MODE": "0"}).safe_mode)
        for value in ("true", "", "2", "False"):
            with self.assertRaises(ValueError):
                self.load({"safe_mode": value})
        with self.assertRaises(ValueError):
            self.load({"ERPNEXT_MAX_CONNECTIONS": "0.5"})

    def test_missing_explicit_env_never_falls_back_to_production(self):
        with patch.dict("os.environ", {"ERPNEXT_ENV_FILE": "__no_such_env_file__"}):
            with self.assertRaisesRegex(ValueError, "refusing fallback"):
                _load_env_values()

    def test_file_takes_precedence_over_stale_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("safe_mode=1\nERPNEXT_URL=https://local.example\n")
            with patch.dict("os.environ", {"ERPNEXT_ENV_FILE": str(path), "safe_mode": "0"}):
                values, _ = _load_env_values()
                self.assertEqual("1", values["safe_mode"])
                self.assertEqual("https://local.example", values["ERPNEXT_URL"])

    def test_bearer_identity_and_invalid_urls(self):
        settings = self.load({"ERPNEXT_ACCESS_TOKEN": "oauth"})
        self.assertNotEqual(settings.identity, replace(settings, access_token="other").identity)
        for url in ("ftp://host", "https://user:password@host", "https://host?key=secret"):
            with self.assertRaises(ValueError):
                self.load({"ERPNEXT_URL": url})


class RequestTests(unittest.TestCase):
    def test_same_origin_and_managed_headers(self):
        for path in (
            "https://other.example/api",
            "//other.example/api",
            "/api/../private",
            "/%252e%252e/private",
            "/\\other.example",
            "/api?cmd=delete",
        ):
            with self.assertRaises(ValueError):
                RequestSpec(method="POST", path=path)
        for header in ("Authorization", "Host", "Cookie", "Content-Length"):
            with self.assertRaises(ValueError):
                RequestSpec(method="POST", path="/api/method/x", headers={header: "bad"})

    def test_unknown_get_is_sensitive_and_custom_doctype_is_not_denied(self):
        unknown = RequestSpec(method="GET", path="/api/method/custom_app.delete_everything")
        self.assertEqual("sensitive", risk_of(unknown.model_dump())["level"])
        custom = RequestSpec(
            method="POST", path="/api/resource/My%20Custom%20Doc", json_body={"title": "x"}
        )
        self.assertEqual("write", risk_of(custom.model_dump())["level"])
        self.assertEqual(
            "sensitive",
            risk_of(
                RequestSpec(
                    method="PUT", path="/api/v2/document/Task/T1", json_body={"items": []}
                ).model_dump()
            )["level"],
        )
        override = RequestSpec(
            method="GET", path="/api/resource/Task", params={"cmd": "custom.delete"}
        )
        self.assertEqual("sensitive", risk_of(override.model_dump())["level"])

    def test_no_silent_unknown_arguments_or_ambiguous_body(self):
        with self.assertRaises(ValueError):
            RequestSpec(method="POST", path="/api/method/x", json_body={}, form={})
        with self.assertRaises(ValueError):
            RequestSpec.model_validate({"method": "POST", "path": "/api/method/x", "risk": "read"})


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            erpnext_url="https://erpnext.example",
            api_key="key",
            api_secret="secret",
            state_dir=self.temp.name,
            inline_result_bytes=1500,
        )
        self.modified = "2026-09-07 01:00:00"
        self.sent = []
        self.behavior = "normal"
        self.deleted = False
        self.client = ERPNextClient(self.settings)
        await self.client._client.aclose()
        self.client._client = httpx.AsyncClient(
            base_url=self.settings.erpnext_url,
            headers=self.client._headers,
            transport=httpx.MockTransport(self.handle),
        )
        self.engine = ExecutionEngine(self.settings, self.client)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def handle(self, request):
        self.sent.append(request)
        path = request.url.path
        if path.startswith("/api/resource/DocType/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": path.rsplit("/", 1)[1],
                        "fields": [
                            {"fieldname": "title", "fieldtype": "Data"},
                            {"fieldname": "items", "fieldtype": "Table", "options": "Custom Child"},
                        ],
                    }
                },
            )
        if path == "/api/resource/Custom Field":
            return httpx.Response(200, json={"data": []})
        if path == "/api/method/frappe.client.get_value":
            return httpx.Response(200, json={"message": None if self.deleted else {"name": "T1"}})
        if request.method == "GET" and path == "/api/resource/Task/T1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "doctype": "Task",
                        "name": "T1",
                        "modified": self.modified,
                        "docstatus": 0,
                        "title": "old",
                    }
                },
            )
        if self.behavior == "timeout":
            raise httpx.ReadTimeout("response lost after commit", request=request)
        if self.behavior == "fail" or path.endswith("/fail"):
            return httpx.Response(417, json={"exc_type": "ValidationError", "exception": "invalid"})
        if self.behavior == "binary":
            return httpx.Response(
                200,
                content=b"\x00\xffPDF" + b"abc" * 2000,
                headers={"content-type": "application/pdf"},
            )
        if self.behavior == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example/steal"})
        if request.method == "DELETE":
            self.deleted = True
            return httpx.Response(200, json={"message": "ok"})
        if path == "/api/resource/Task" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "doctype": "Task",
                        "name": "T1",
                        "modified": self.modified,
                        "docstatus": 0,
                        **json.loads(request.content),
                    }
                },
            )
        return httpx.Response(200, json={"message": {"accepted": True, "text": "中文" * 2000}})

    def mutations(self):
        return [
            r
            for r in self.sent
            if r.method != "GET" and r.url.path != "/api/method/frappe.client.get_value"
        ]

    async def test_default_create_preflights_and_executes_without_extra_confirmation(self):
        result = await self.engine.request(
            RequestSpec(method="POST", path="/api/resource/Task", json_body={"title": "new"})
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, len(self.mutations()))
        self.assertTrue(result["steps"][0]["readback"]["verified"])

    async def test_unknown_rpc_dry_run_does_not_call_it_and_execution_uses_frozen_request(self):
        spec = RequestSpec(
            method="POST", path="/api/method/custom.submit", json_body={"values": [1, 2, 3]}
        )
        prepared = await self.engine.request(spec, dry_run=True)
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual([], self.sent)
        spec.json_body["values"].pop()
        no_consent = await self.engine.execute(prepared["plan_id"], prepared["request_sha256"])
        self.assertEqual("prepared", no_consent["status"])
        done = await self.engine.execute(
            prepared["plan_id"], prepared["request_sha256"], "同意执行这批数据"
        )
        self.assertEqual("completed", done["status"])
        self.assertEqual([1, 2, 3], json.loads(self.sent[0].content)["values"])
        await self.engine.execute(prepared["plan_id"], prepared["request_sha256"], "同意")
        self.assertEqual(1, len(self.sent))

    async def test_hash_mismatch_and_cross_target_never_execute(self):
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        with self.assertRaisesRegex(ValueError, "hash"):
            await self.engine.execute(plan["plan_id"], "0" * 64, "approved")
        for settings in (
            replace(self.settings, erpnext_url="https://other.example"),
            replace(self.settings, api_key="another-user"),
        ):
            other = ExecutionEngine(settings, self.client)
            with self.assertRaisesRegex(ValueError, "target/identity"):
                await other.execute(plan["plan_id"], plan["request_sha256"], "approved")
        self.assertEqual([], self.sent)

    async def test_stale_plan_blocks_all_mutations(self):
        plan = await self.engine.request(
            RequestSpec(method="DELETE", path="/api/resource/Task/T1"), dry_run=True
        )
        self.modified = "2026-09-07 02:00:00"
        result = await self.engine.execute(plan["plan_id"], plan["request_sha256"], "删除 T1")
        self.assertEqual("conflict", result["status"])
        self.assertEqual([], self.mutations())

    async def test_expiration_and_running_state_survive_restart(self):
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        with self.engine.store.connect() as db:
            db.execute("UPDATE plans SET expires=0 WHERE id=?", (plan["plan_id"],))
        with self.assertRaisesRegex(ValueError, "expired"):
            await self.engine.execute(plan["plan_id"], plan["request_sha256"], "approved")
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        self.engine.store.claim(plan["plan_id"], plan["request_sha256"], "approved")
        restarted = ExecutionEngine(self.settings, self.client)
        result = await restarted.execute(plan["plan_id"], plan["request_sha256"], "approved")
        self.assertEqual("running", result["status"])
        self.assertEqual([], self.sent)

    async def test_one_confirmation_covers_batch_and_failure_stops_remaining_requests(self):
        specs = [
            RequestSpec(method="POST", path="/api/method/" + m) for m in ("one", "fail", "three")
        ]
        plan = await self.engine.prepare(specs, expected_count=3)
        result = await self.engine.execute(plan["plan_id"], plan["request_sha256"], "批准全部三步")
        self.assertEqual("failed", result["status"])
        self.assertEqual(2, len(self.sent))
        self.assertIsNone(result["transaction_rolled_back"])
        self.assertTrue(result["partial_execution"])

    async def test_timeout_is_unknown_and_never_retried(self):
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        self.behavior = "timeout"
        result = await self.engine.execute(plan["plan_id"], plan["request_sha256"], "approved")
        self.assertEqual("unknown", result["status"])
        await self.engine.execute(plan["plan_id"], plan["request_sha256"], "approved")
        self.assertEqual(1, len(self.sent))

    async def test_safe_mode_zero_skips_confirmation_and_business_preflight_only(self):
        direct = ExecutionEngine(replace(self.settings, safe_mode=False), self.client)
        result = await direct.request(RequestSpec(method="POST", path="/api/method/x"))
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, len(self.sent))
        with self.assertRaisesRegex(ValueError, "completeness"):
            await direct.request(
                RequestSpec(
                    method="POST",
                    path="/api/method/x",
                    json_body={"rows": [1]},
                    expected_lengths={"/rows": 2},
                )
            )
        self.assertEqual(1, len(self.sent))

    async def test_config_disable_does_not_silently_downgrade_existing_plan(self):
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        direct = ExecutionEngine(replace(self.settings, safe_mode=False), self.client)
        result = await direct.execute(plan["plan_id"], plan["request_sha256"])
        self.assertTrue(result["confirmation_required"])
        self.assertEqual([], self.sent)

    async def test_binary_response_and_large_json_are_complete_and_retrievable(self):
        self.behavior = "binary"
        result = await self.engine.request(RequestSpec(method="GET", path="/api/resource/Task"))
        body = self.engine.store.get(result["body"]["artifact_id"])
        self.assertEqual(b"\x00\xffPDF" + b"abc" * 2000, body)
        self.behavior = "normal"
        result = await self.engine.request(RequestSpec(method="GET", path="/api/resource/Task"))
        full = json.loads(self.engine.store.get(result["body"]["artifact_id"]))
        self.assertEqual("中文" * 2000, full["message"]["text"])
        self.assertNotIn("data", result)

    async def test_redirect_does_not_forward_credentials(self):
        self.behavior = "redirect"
        result = await self.engine.request(RequestSpec(method="GET", path="/api/resource/Task"))
        self.assertEqual(302, result["http_status"])
        self.assertEqual(1, len(self.sent))

    async def test_dependent_create_submit_freezes_binding_and_confirms_once(self):
        specs = [
            RequestSpec(method="POST", path="/api/resource/Task", json_body={"title": "new"}),
            RequestSpec(
                method="POST",
                path="/api/method/frappe.client.submit",
                json_body={"doc": None},
                bindings={"/doc": ResultBinding(step=0, pointer="/data")},
            ),
        ]
        plan = await self.engine.prepare(specs, expected_count=2)
        self.assertTrue(plan["confirmation_required"])
        await self.engine.execute(plan["plan_id"], plan["request_sha256"], "创建并提交")
        submitted = [r for r in self.sent if r.url.path.endswith("frappe.client.submit")][0]
        self.assertEqual("T1", json.loads(submitted.content)["doc"]["name"])

    async def test_batch_count_mismatch_and_future_binding_rejected(self):
        with self.assertRaises(ValueError):
            await self.engine.prepare(
                [RequestSpec(method="GET", path="/api/resource/Task")], expected_count=2
            )
        with self.assertRaises(ValueError):
            await self.engine.prepare(
                [
                    RequestSpec(
                        method="POST",
                        path="/api/method/x",
                        json_body={"doc": None},
                        bindings={"/doc": ResultBinding(step=0, pointer="/data")},
                    )
                ],
                expected_count=1,
            )

    async def test_concurrent_execute_only_claims_once(self):
        plan = await self.engine.prepare(
            [RequestSpec(method="POST", path="/api/method/x")], expected_count=1
        )
        await asyncio.gather(
            *(
                self.engine.execute(plan["plan_id"], plan["request_sha256"], "approved")
                for _ in range(2)
            )
        )
        self.assertEqual(1, len(self.sent))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(erpnext_url="https://erpnext.example", state_dir=self.temp.name)
        self.store = StateStore(self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def test_incomplete_wrong_hash_and_reordered_chunks_cannot_execute(self):
        source = canonical({"rows": ["中文", "完整"]})
        upload = self.store.begin_upload(len(source), digest(source), "application/json")[
            "artifact_id"
        ]
        with self.assertRaises(ValueError):
            self.store.get(upload)
        with self.assertRaises(ValueError):
            self.store.append(upload, 1, base64.b64encode(source[:3]).decode())
        self.store.append(upload, 0, base64.b64encode(source[:3]).decode())
        with self.assertRaises(ValueError):
            self.store.finalize(upload)
        self.store.append(upload, 3, base64.b64encode(source[3:]).decode())
        self.store.finalize(upload)
        self.assertEqual(source, self.store.get(upload))
        with self.assertRaises(ValueError):
            self.store.append(upload, len(source), "")
        bad = self.store.begin_upload(1, "0" * 64, "text/plain")["artifact_id"]
        self.store.append(bad, 0, "YQ==")
        with self.assertRaises(ValueError):
            self.store.finalize(bad)

    def test_chunk_read_reconstructs_exact_utf8_and_enforces_target(self):
        source = canonical({"text": "中文" * 5000})
        identifier = self.store.put(source)["artifact_id"]
        result = bytearray()
        offset = 0
        while True:
            chunk = self.store.read(identifier, offset=offset, limit=1001)
            result.extend(base64.b64decode(chunk["data"]))
            if not chunk["has_more"]:
                break
            offset = chunk["next_offset"]
        self.assertEqual(source, bytes(result))
        other = StateStore(replace(self.settings, erpnext_url="https://other.example"))
        with self.assertRaises(ValueError):
            other.get(identifier)

    def test_plan_list_recovers_receipts_without_reexecuting(self):
        payload = {"steps": [], "label": "test recovery"}
        first = self.store.prepare(payload)
        second = self.store.prepare(payload)
        self.store.claim(first["plan_id"], first["request_sha256"], "approved")
        self.store.record(first["plan_id"], "unknown", {"completed_count": 0})
        listed = self.store.list_plans(status="unknown")
        self.assertEqual(first["plan_id"], listed["plans"][0]["plan_id"])
        self.assertEqual(1, listed["total_count"])
        self.assertNotEqual(first["plan_id"], second["plan_id"])


class TransportAndProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = EngineTests.asyncSetUp
    asyncTearDown = EngineTests.asyncTearDown
    handle = EngineTests.handle

    async def test_raw_null_form_repeated_queries_and_multipart_preserve_content(self):
        direct = ExecutionEngine(replace(self.settings, safe_mode=False), self.client)
        await direct.request(
            RequestSpec(method="POST", path="/api/method/x", json_body=[1, {"text": "中文"}])
        )
        self.assertEqual([1, {"text": "中文"}], json.loads(self.sent[-1].content))
        await direct.request(RequestSpec(method="POST", path="/api/method/x", json_body=None))
        self.assertEqual(b"null", self.sent[-1].content)
        await direct.request(
            RequestSpec(method="POST", path="/api/method/x", form={"text": "中文", "count": "2"})
        )
        self.assertIn(b"text=%E4%B8%AD%E6%96%87", self.sent[-1].content)
        await direct.request(
            RequestSpec(
                method="PATCH",
                path="/api/v2/document/Custom/C1",
                body_base64=base64.b64encode(b"\x00\xffraw").decode(),
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        self.assertEqual(b"\x00\xffraw", [r for r in self.sent if r.method == "PATCH"][-1].content)
        await direct.request(
            RequestSpec(
                method="GET", path="/api/resource/Task", params=[("tag", "a"), ("tag", "b")]
            )
        )
        self.assertEqual(["a", "b"], self.sent[-1].url.params.get_list("tag"))
        from erpnext_mcp.requests import FilePart

        source = b"\x00\xfffile-content"
        artifact = self.engine.store.put(source, "application/octet-stream")["artifact_id"]
        await direct.request(
            RequestSpec(
                method="POST",
                path="/api/method/upload_file",
                form={"is_private": "1"},
                files=[FilePart(filename="test.bin", artifact_id=artifact)],
            )
        )
        self.assertIn(source, self.sent[-1].content)
        self.assertIn(b'filename="test.bin"', self.sent[-1].content)
        self.assertTrue(
            self.sent[-1].headers["content-type"].startswith("multipart/form-data; boundary=")
        )

    async def test_finalized_json_artifact_and_size_limit_are_enforced(self):
        source = canonical({"rows": [1, 2, 3]})
        artifact = self.engine.store.put(source)["artifact_id"]
        plan = await self.engine.request(
            RequestSpec(
                method="POST",
                path="/api/method/x",
                json_artifact_id=artifact,
                expected_lengths={"/rows": 3},
            ),
            dry_run=True,
        )
        self.assertEqual(
            [1, 2, 3],
            self.engine.store.plan(plan["plan_id"])["payload"]["steps"][0]["request"]["json_body"][
                "rows"
            ],
        )
        with self.assertRaises(ValueError):
            await self.engine.request(
                RequestSpec(
                    method="POST",
                    path="/api/method/x",
                    json_artifact_id=artifact,
                    expected_lengths={"/rows": 4},
                ),
                dry_run=True,
            )
        limited = ExecutionEngine(replace(self.settings, max_artifact_bytes=1000), self.client)
        with self.assertRaises(ValueError):
            await limited.prepare(
                [RequestSpec(method="POST", path="/api/method/x", json_body={"text": "x" * 2000})],
                expected_count=1,
            )

    async def test_v2_method_and_error_status_are_preserved(self):
        direct = ExecutionEngine(replace(self.settings, safe_mode=False), self.client)
        result = await direct.request(
            RequestSpec(
                method="PATCH", path="/api/v2/document/Custom%20Doc/C1", json_body={"title": "x"}
            )
        )
        self.assertEqual("completed", result["status"])
        sent = [r for r in self.sent if r.method == "PATCH"][-1]
        self.assertEqual("/api/v2/document/Custom Doc/C1", sent.url.path)
        self.behavior = "fail"
        result = await direct.request(RequestSpec(method="POST", path="/api/method/custom.denied"))
        step = result["steps"][0]
        self.assertEqual(417, step["http_status"])
        full = json.loads(self.engine.store.get(step["body"]["artifact_id"]))
        self.assertEqual("ValidationError", full["exc_type"])
        self.assertIsNone(step["transaction_rolled_back"])

    async def test_mcp_schema_confirmation_flow_and_nonrecursive_result_chunks(self):
        from fastmcp import Client
        from erpnext_mcp import server
        from erpnext_mcp.runtime import invocation

        def context():
            return invocation(self.settings, self.client)

        with patch("erpnext_mcp.server.invocation", context):
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                self.assertIn("erpnext_api_request", names)
                self.assertIn("erpnext_plan_execute", names)
                # The nested typed RequestSpec must survive MCP JSON schema validation.
                result = await client.call_tool(
                    "erpnext_api_request",
                    {
                        "request": {
                            "method": "POST",
                            "path": "/api/method/custom.action",
                            "json_body": {"rows": [1, 2]},
                        },
                        "dry_run": True,
                    },
                )
                plan = result.data
                self.assertTrue(plan["confirmation_required"])
                self.assertEqual([], self.sent)
                result = await client.call_tool(
                    "erpnext_plan_execute",
                    {
                        "plan_id": plan["plan_id"],
                        "request_sha256": plan["request_sha256"],
                        "user_confirmation": "确认这个测试批次",
                    },
                )
                self.assertEqual("completed", result.data["status"])
                self.assertEqual(1, len(self.sent))
                source = canonical({"text": "中文" * 5000})
                identifier = self.engine.store.put(source)["artifact_id"]
                chunk = await client.call_tool(
                    "erpnext_result_read", {"artifact_id": identifier, "offset": 0, "limit": 4096}
                )
                self.assertEqual(source[:4096], base64.b64decode(chunk.data["data"]))
                self.assertNotIn("result_id", chunk.data)

    async def test_sensitive_conveniences_have_no_legacy_direct_write_path(self):
        from fastmcp import Client
        from erpnext_mcp import server
        from erpnext_mcp.runtime import invocation

        def context():
            return invocation(self.settings, self.client)

        with patch("erpnext_mcp.server.invocation", context):
            async with Client(server.mcp) as client:
                with self.assertRaisesRegex(Exception, "extra_args"):
                    await client.call_tool(
                        "erpnext_doc_map",
                        {
                            "source_doctype": "Sales Order",
                            "source_name": "SO-TEST",
                            "target_doctype": "Sales Invoice",
                            "extra_args": {"cmd": "custom.delete"},
                        },
                    )
                self.assertEqual([], self.sent)
                for tool in ("erpnext_doc_submit", "erpnext_doc_cancel", "erpnext_doc_delete"):
                    self.sent.clear()
                    result = await client.call_tool(
                        tool, {"doctype": "Task", "name": "T1", "dry_run": False}
                    )
                    self.assertTrue(result.data["confirmation_required"], tool)
                    self.assertFalse(
                        any(r.method in ("POST", "PUT", "DELETE") for r in self.sent), tool
                    )
                self.sent.clear()
                result = await client.call_tool(
                    "erpnext_naming_series_configure",
                    {
                        "doctype": "Sales Order",
                        "series_options": ["SO-.YYYY.-.#####"],
                        "counters": {"SO-.YYYY.-.#####": 1},
                        "dry_run": False,
                    },
                )
                self.assertTrue(result.data["confirmation_required"])
                self.assertFalse(any(r.method == "POST" for r in self.sent))
