"""Explicit-target, read-only live smoke checks. Never create/update/delete a business record."""

from __future__ import annotations

import argparse
import asyncio
import json

from . import business
from .engine import ExecutionEngine
from .requests import RequestSpec
from .runtime import invocation


async def run(expected_url: str) -> dict:
    async with invocation() as (settings, client):
        if settings.erpnext_url != expected_url.rstrip("/"):
            raise ValueError("Resolved target differs from --expect-url; no ERPNext request sent")
        health = await business.erpnext_health_check()
        selected = ExecutionEngine(settings, client)
        v1 = await selected.request(
            RequestSpec(method="GET", path="/api/resource/DocType", params={"limit_page_length": 1})
        )
        v2 = await selected.request(
            RequestSpec(
                method="GET", path="/api/v2/document/DocType", params={"limit_page_length": 1}
            )
        )
        prepared = await selected.request(
            RequestSpec(
                method="POST",
                path="/api/method/nonexistent_app.preflight_only",
                json_body={"rows": [1, 2]},
                expected_lengths={"/rows": 2},
            ),
            dry_run=True,
        )
        return {
            "target": settings.erpnext_url,
            "safe_mode": int(settings.safe_mode),
            "health": health,
            "v1_status": v1.get("http_status"),
            "v2_status": v2.get("http_status"),
            "unknown_method_prepared": prepared["status"] == "prepared",
            "remote_business_writes": 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-url", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.expect_url)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
