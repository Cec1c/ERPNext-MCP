from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from fastmcp import Client


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_entrypoint_handshake_and_offline_safe_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text(
                "ERPNEXT_URL=https://never-contact.invalid\n"
                "ERPNEXT_API_KEY=fixture-key\nERPNEXT_API_SECRET=fixture-secret\n"
                "safe_mode=1\nERPNEXT_STATE_DIR=.state\n",
                encoding="utf-8",
            )
            config = {
                "mcpServers": {
                    "erpnext": {
                        "command": sys.executable,
                        "args": ["-B", "-u", "-m", "erpnext_mcp"],
                        "env": {"ERPNEXT_ENV_FILE": str(env)},
                    }
                }
            }
            async with Client(config) as client:
                tools = await client.list_tools()
                self.assertEqual(31, len(tools))
                status = (await client.call_tool("erpnext_config_status", {})).data
                self.assertEqual("https://never-contact.invalid", status["url"])
                self.assertEqual(1, status["safe_mode"])
                self.assertFalse(status["mcp_allowlist_enabled"])
                prepared = (
                    await client.call_tool(
                        "erpnext_call_method",
                        {
                            "method": "custom.fixture.no_network",
                            "args": {"rows": [1, 2]},
                            "dry_run": True,
                        },
                    )
                ).data
                self.assertEqual("prepared", prepared["status"])
                self.assertTrue(prepared["confirmation_required"])
                listed = (await client.call_tool("erpnext_plan_list", {})).data
                self.assertEqual(prepared["plan_id"], listed["plans"][0]["plan_id"])

    async def test_two_profiles_start_concurrently_from_shared_environment(self):
        with tempfile.TemporaryDirectory() as directory:

            async def probe(profile):
                env = Path(directory) / f".env.{profile}"
                env.write_text(
                    f"ERPNEXT_URL=https://{profile}.invalid\n"
                    "ERPNEXT_API_KEY=fixture-key\nERPNEXT_API_SECRET=fixture-secret\n"
                    "safe_mode=1\nERPNEXT_STATE_DIR=.state\n",
                    encoding="utf-8",
                )
                config = {
                    "mcpServers": {
                        profile: {
                            "command": sys.executable,
                            "args": ["-B", "-u", "-m", "erpnext_mcp"],
                            "env": {"ERPNEXT_ENV_FILE": str(env)},
                        }
                    }
                }
                async with Client(config) as client:
                    self.assertEqual(31, len(await client.list_tools()))
                    status = (await client.call_tool("erpnext_config_status", {})).data
                    self.assertEqual(f"https://{profile}.invalid", status["url"])
                    self.assertEqual("0.2.0", status["version"])

            await asyncio.gather(probe("erpnext_test"), probe("erpnext_prod"))
