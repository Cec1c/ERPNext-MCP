"""Prepare, review and execute immutable HTTP plans through one safety boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from copy import deepcopy

import httpx

from .client import ERPNextClient, ERPNextError
from .config import Settings
from .requests import RequestSpec, canonical, digest, document_target, pointer, risk_of
from .state import StateStore


class ExecutionEngine:
    def __init__(self, settings: Settings, client: ERPNextClient, store: StateStore | None = None):
        self.settings, self.client = settings, client
        self.store = store or StateStore(settings)

    def resolve(self, spec: RequestSpec) -> dict:
        request = spec.model_dump()
        request["json_provided"] = "json_body" in spec.model_fields_set or bool(
            request["json_artifact_id"]
        )
        if request["json_artifact_id"]:
            request["json_body"] = json.loads(self.store.get(request["json_artifact_id"]))
            request["json_artifact_id"] = None
        if request["body_artifact_id"]:
            request["body_base64"] = base64.b64encode(
                self.store.get(request["body_artifact_id"])
            ).decode()
            request["body_artifact_id"] = None
        for file in request["files"]:
            self.store.get(file["artifact_id"])
        for path, count in request["expected_lengths"].items():
            if any(
                path == target or path.startswith(target + "/")
                for target in request.get("bindings", {})
            ):
                continue
            value = pointer(request["json_body"], path)
            if not isinstance(value, list) or len(value) != count:
                raise ValueError(
                    f"Input completeness check failed at {path}: expected {count} rows"
                )
        if len(canonical(request)) > self.settings.max_artifact_bytes:
            raise ValueError(
                "Request exceeds configured artifact size; split into explicit batches"
            )
        return request

    async def _preflight(self, request: dict) -> tuple[dict, list[dict], dict]:
        risk = risk_of(request)
        conditions: list[dict] = []
        validation: dict = {
            "scope": "request_integrity",
            "business_execution_guaranteed": False,
            "issues": [],
            "not_verified": ["Server hooks and external side effects"],
        }
        dt, name = document_target(request)
        body = request.get("json_body")
        doc = body if dt and isinstance(body, dict) else None
        rpc = request["path"].split("/method/", 1)[-1]
        if isinstance(body, dict) and rpc in {
            "frappe.client.insert",
            "frappe.client.save",
            "frappe.client.submit",
            "frappe.client.cancel",
        }:
            doc = body.get("doc")
            if isinstance(doc, str):
                try:
                    doc = json.loads(doc)
                except ValueError:
                    doc = None
            if isinstance(doc, dict):
                dt, name = doc.get("doctype"), doc.get("name")
                if rpc == "frappe.client.insert":
                    name = None
            elif rpc == "frappe.client.cancel":
                dt, name = body.get("doctype"), body.get("name")
        if dt and name and risk["level"] != "read":
            current = await self.client.get_doc(dt, name)
            expected = request.get("expected_modified")
            if expected is not None and current.get("modified") != expected:
                raise ValueError("Document changed before preflight; inspect the current document")
            if (
                isinstance(doc, dict)
                and doc.get("modified")
                and doc["modified"] != current.get("modified")
            ):
                raise ValueError("Payload contains a stale modified timestamp")
            if current.get("modified"):
                conditions.append({"doctype": dt, "name": name, "modified": current["modified"]})
            if current.get("docstatus"):
                risk.update(level="sensitive", reason="Mutation of a submitted/cancelled document")
            if isinstance(doc, dict):
                validation["child_table_changes"] = {
                    k: {"before": len(current.get(k) or []), "after": len(v)}
                    for k, v in doc.items()
                    if isinstance(v, list)
                }
                proposed = {**current, **doc}
            else:
                proposed = None
        else:
            proposed = doc
        if dt and proposed is not None and risk["level"] != "read":
            # Client-side schemas cannot model server defaults or custom controllers exactly.
            # Their findings are review material, not a replacement admission whitelist.
            from .business import _validate_doc_payload

            try:
                checks = await _validate_doc_payload(
                    dt, proposed, client=self.client, settings=self.settings
                )
                validation["schema_and_known_rules"] = checks
                validation["scope"] = "integrity_schema_links_and_known_rules"
                if not checks["valid"]:
                    validation["issues"].append(
                        "Preflight found issues; review before attempting server execution"
                    )
                    risk.update(level="sensitive", reason="Preflight findings require review")
            except (ERPNextError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                validation["issues"].append(
                    f"Metadata validation unavailable: {type(exc).__name__}"
                )
                risk.update(
                    level="sensitive", reason="Server business validation could not be completed"
                )
        return risk, conditions, validation

    async def prepare(
        self,
        specs: list[RequestSpec],
        *,
        expected_count: int,
        label: str = "ERPNext operation",
        force_sensitive: bool = False,
        extra_conditions: list[dict] | None = None,
    ) -> dict:
        if not specs or len(specs) != expected_count:
            raise ValueError("Batch length does not match expected_count, or batch is empty")
        requests = [self.resolve(s) for s in specs]
        steps, conditions = [], list(extra_conditions or [])
        for index, request in enumerate(requests):
            for target, binding in request.get("bindings", {}).items():
                if binding["step"] >= index or not target.startswith("/"):
                    raise ValueError(
                        "Bindings must target a JSON body pointer and reference an earlier step"
                    )
            risk, required, validation = await self._preflight(request)
            conditions.extend(required)
            steps.append({"request": request, "risk": risk, "validation": validation})
        sensitive = force_sensitive or any(s["risk"]["level"] == "sensitive" for s in steps)
        if len(steps) > 1 and any(s["risk"]["level"] != "read" for s in steps):
            sensitive = True
        payload = {
            "target": self.settings.erpnext_url,
            "site": self.settings.site,
            "safe_mode": self.settings.safe_mode,
            "label": label,
            "steps": steps,
            "conditions": conditions,
            "sensitive": sensitive,
            "expected_count": expected_count,
            "preconditions_atomic": False,
        }
        plan = self.store.prepare(payload)
        return self.describe(plan)

    def describe(self, plan: dict) -> dict:
        payload = plan["payload"]
        summary = [
            {
                "index": i,
                "method": s["request"]["method"],
                "path": s["request"]["path"],
                "risk": s["risk"],
                "validation": s["validation"],
                "expected_lengths": s["request"].get("expected_lengths", {}),
                "bindings": s["request"].get("bindings", {}),
                "request_bytes": len(canonical(s["request"])),
            }
            for i, s in enumerate(payload["steps"])
        ]
        return {
            "ok": True,
            "plan_id": plan["plan_id"],
            "request_sha256": plan["request_sha256"],
            "status": plan["status"],
            "expires_at": plan["expires_at"],
            "target": payload["target"],
            "site": payload.get("site"),
            "label": payload["label"],
            "operation_count": len(summary),
            "steps": summary,
            "confirmation_required": payload["sensitive"]
            and (self.settings.safe_mode or payload["safe_mode"]),
            "confirmation_source": "agent_attestation",
            "agent_instruction": "Show the target, exact scope and findings to the user. For a sensitive plan, ask once for this entire plan and pass their actual approval text to erpnext_plan_execute. Do not invent approval or repeat an already obtained in-scope approval.",
            "request_review": "erpnext_plan_inspect returns the frozen request artifact",
            "business_execution_guaranteed": False,
            "result": plan["result"],
        }

    async def request(self, spec: RequestSpec, *, dry_run: bool = False) -> dict:
        request = self.resolve(spec)
        risk = risk_of(request)
        if request.get("bindings"):
            raise ValueError("Response bindings require erpnext_batch_prepare")
        if risk["level"] == "read" and not dry_run:
            return await self.client.send(request, self.store)
        if not self.settings.safe_mode and not dry_run:
            # Still persist an execution receipt and protect against plan replay.
            payload = {
                "target": self.settings.erpnext_url,
                "safe_mode": False,
                "label": "Direct API call",
                "steps": [
                    {"request": request, "risk": risk, "validation": {"scope": "request_integrity"}}
                ],
                "conditions": [],
                "sensitive": risk["level"] == "sensitive",
                "expected_count": 1,
            }
            plan = self.store.prepare(payload)
            return await self.execute(plan["plan_id"], plan["request_sha256"])
        prepared = await self.prepare([spec], expected_count=1)
        if dry_run or prepared["confirmation_required"]:
            return prepared
        return await self.execute(prepared["plan_id"], prepared["request_sha256"])

    async def execute(
        self, identifier: str, request_sha256: str, user_confirmation: str | None = None
    ) -> dict:
        plan = self.store.plan(identifier)
        if request_sha256 != plan["request_sha256"]:
            raise ValueError("Approval hash does not match the frozen plan")
        if plan["status"] != "prepared":
            return {
                "ok": plan["status"] == "completed",
                "plan_id": identifier,
                "status": plan["status"],
                "replayed": True,
                "message": "No request re-executed. A running/unknown plan requires reconciliation.",
                "result": plan["result"],
            }
        if plan["expires_at"] <= time.time():
            raise ValueError("Plan expired; prepare and review current state again")
        payload = plan["payload"]
        if payload["sensitive"] and (self.settings.safe_mode or payload["safe_mode"]):
            if not user_confirmation or not user_confirmation.strip():
                return self.describe(plan)
            if len(user_confirmation) > 4096:
                raise ValueError("Confirmation text exceeds 4096 characters")
        if not self.store.claim(identifier, request_sha256, user_confirmation):
            return {
                "ok": False,
                "plan_id": identifier,
                "status": "already_claimed",
                "message": "Read plan status; do not create a replacement request to retry.",
            }
        results: list[dict] = []
        try:
            # Resolve every attachment and verify every precondition before the first mutation.
            for step in payload["steps"]:
                for file in step["request"].get("files", []):
                    self.store.get(file["artifact_id"])
            for condition in payload["conditions"]:
                doc = await self.client.get_doc(condition["doctype"], condition["name"])
                if doc.get("modified") != condition["modified"]:
                    result = {
                        "ok": False,
                        "plan_id": identifier,
                        "status": "conflict",
                        "completed_count": 0,
                        "message": "Document changed since preflight",
                        "document": {k: condition[k] for k in ("doctype", "name")},
                    }
                    self.store.record(identifier, "conflict", result)
                    return result
            for index, step in enumerate(payload["steps"]):
                actual = deepcopy(step["request"])
                for target, binding in actual.get("bindings", {}).items():
                    previous = results[binding["step"]]
                    raw = json.loads(self.store.get(previous["body"]["artifact_id"]))
                    value = pointer(raw, binding["pointer"])
                    container = actual["json_body"]
                    tokens = target[1:].split("/")
                    for token in tokens[:-1]:
                        token = token.replace("~1", "/").replace("~0", "~")
                        container = (
                            container[int(token)]
                            if isinstance(container, list)
                            else container[token]
                        )
                    key = tokens[-1].replace("~1", "/").replace("~0", "~")
                    container[int(key) if isinstance(container, list) else key] = value
                # Recheck content assertions after resolving deterministic prior-response bindings.
                for path, count in actual.get("expected_lengths", {}).items():
                    if len(pointer(actual["json_body"], path)) != count:
                        raise ValueError(
                            "Resolved request length differs from approved expectation"
                        )
                response = await self.client.send(actual, self.store)
                if step["risk"]["level"] != "read":
                    from .business import _METADATA_CACHE

                    _METADATA_CACHE.clear_target(self.settings)
                results.append(
                    {
                        "index": index,
                        "executed_request_sha256": digest(canonical(actual)),
                        **response,
                    }
                )
                snapshot = {
                    "plan_id": identifier,
                    "completed_count": sum(bool(r["ok"]) for r in results),
                    "attempted_count": len(results),
                    "steps": results,
                }
                self.store.record(identifier, "running", snapshot)
                if not response["ok"]:
                    result = {
                        **snapshot,
                        "ok": False,
                        "status": "failed",
                        "partial_execution": len(results) > 1,
                        "transaction_rolled_back": None,
                        "message": "Stopped at the first upstream error; earlier requests are not rolled back.",
                    }
                    self.store.record(identifier, "failed", result)
                    return result
                try:
                    results[-1]["readback"] = await self._readback(step["request"], response)
                except (ERPNextError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    results[-1]["readback"] = {
                        "verified": False,
                        "error_type": type(exc).__name__,
                        "message": "Request succeeded; readback could not be verified",
                    }
            result = {
                "ok": True,
                "plan_id": identifier,
                "status": "completed",
                "request_sha256": request_sha256,
                "completed_count": len(results),
                "steps": results,
                "batch_atomic": False,
                "verification_complete": all(
                    r.get("readback", {}).get("verified") is True for r in results
                ),
            }
            self.store.record(identifier, "completed", result)
            return result
        except BaseException as exc:
            result = {
                "ok": False,
                "plan_id": identifier,
                "status": "unknown",
                "completed_count": len(results),
                "steps": results,
                "error_type": type(exc).__name__,
                "transaction_rolled_back": None,
                "message": "Execution interrupted. Reconcile upstream state before issuing any replacement plan.",
            }
            self.store.record(identifier, "unknown", result)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result

    async def _readback(self, request: dict, response: dict) -> dict:
        dt, name = document_target(request)
        data = response.get("data")
        if data is None and response.get("body"):
            try:
                data = json.loads(self.store.get(response["body"]["artifact_id"]))
            except (ValueError, UnicodeDecodeError):
                pass
        doc = (data.get("data") or data.get("message")) if isinstance(data, dict) else None
        if isinstance(doc, dict) and doc.get("doctype") and doc.get("name"):
            dt, name = doc["doctype"], doc["name"]
        if not dt or not name:
            return {"verified": None, "scope": "No generic readback for this method/response"}
        if request["method"] == "DELETE":
            exists = await self.client.doc_exists(dt, name)
            return {"verified": not exists, "exists": exists, "doctype": dt, "name": name}
        current = await self.client.get_doc(dt, name)
        if isinstance(doc, dict):
            matched = all(
                current.get(k) == doc.get(k) for k in ("name", "docstatus", "modified") if k in doc
            )
        else:
            matched = None
        return {
            "verified": matched,
            "scope": "Document identity/status/version, not accounting or stock effects",
            "doctype": dt,
            "name": name,
            "docstatus": current.get("docstatus"),
            "modified": current.get("modified"),
        }
