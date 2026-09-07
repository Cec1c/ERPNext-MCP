"""Lossless request descriptions. Risk is computed by MCP, never supplied by an agent."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pointer(value: Any, path: str) -> Any:
    if not path:
        return value
    if not path.startswith("/"):
        raise ValueError("Use an RFC 6901 JSON pointer, starting with /")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class FilePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = "file"
    filename: str
    content_type: str = "application/octet-stream"
    artifact_id: str


class ResultBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(ge=0)
    pointer: str


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str
    params: dict[str, Any] | list[tuple[str, Any]] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = None
    json_artifact_id: str | None = None
    body_base64: str | None = None
    body_artifact_id: str | None = None
    form: dict[str, Any] | None = None
    files: list[FilePart] = Field(default_factory=list)
    expected_lengths: dict[str, int] = Field(default_factory=dict)
    expected_modified: str | None = None
    bindings: dict[str, ResultBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request(self) -> RequestSpec:
        path = self.path
        parsed = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.netloc or parsed.scheme:
            raise ValueError("path must start with / and stay on the configured ERPNext origin")
        if parsed.fragment or parsed.query:
            raise ValueError("Use params for query parameters; fragments are not supported")
        decoded = path
        for _ in range(4):
            decoded = unquote(decoded)
        if "\\" in decoded or any(c in decoded for c in "\r\n\0") or decoded.startswith("//"):
            raise ValueError("Invalid request path")
        if any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("Path traversal is not supported")
        for name, value in self.headers.items():
            if name.lower() in {
                "authorization",
                "proxy-authorization",
                "cookie",
                "host",
                "x-frappe-site-name",
                "content-length",
                "transfer-encoding",
            }:
                raise ValueError(f"Header {name} is managed by the configured connection")
            if any(c in name + value for c in "\r\n\0"):
                raise ValueError("Invalid header")
        bodies = [
            "json_body" in self.model_fields_set,
            self.json_artifact_id is not None,
            self.body_base64 is not None,
            self.body_artifact_id is not None,
            self.form is not None or bool(self.files),
        ]
        if sum(bodies) > 1:
            raise ValueError("Specify only one body encoding")
        if self.body_base64 is not None:
            base64.b64decode(self.body_base64, validate=True)
        if any(isinstance(n, bool) or n < 0 for n in self.expected_lengths.values()):
            raise ValueError("expected_lengths must contain non-negative array lengths")
        canonical(self.model_dump())
        return self


SENSITIVE_DOCTYPES = {
    "User",
    "Role",
    "Has Role",
    "User Permission",
    "Custom DocPerm",
    "DocPerm",
    "System Settings",
    "Stock Settings",
    "Accounts Settings",
    "Document Naming Settings",
    "DocType",
    "Custom Field",
    "Property Setter",
    "Server Script",
    "Client Script",
    "Workflow",
    "Workflow State",
    "Workflow Action Master",
    "Email Account",
    "Email Queue",
    "Communication",
    "Notification",
    "Auto Email Report",
    "DocShare",
    "Stock Reconciliation",
    "GL Entry",
    "Stock Ledger Entry",
}

# Risk catalogue, NOT a capability allowlist: unlisted methods can be confirmed and executed.
READ_METHODS = {
    "frappe.auth.get_logged_user",
    "frappe.utils.change_log.get_versions",
    "frappe.client.get",
    "frappe.client.get_list",
    "frappe.client.get_count",
    "frappe.client.get_value",
    "frappe.client.get_single_value",
    "frappe.desk.search.search_link",
    "frappe.desk.query_report.run",
    "erpnext.accounts.utils.get_balance_on",
    "erpnext.stock.get_item_details.get_bin_details",
    "erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry",
    "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
    "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
    "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
    "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
    "erpnext.accounts.doctype.sales_invoice.sales_invoice.make_delivery_note",
}


def document_target(request: dict) -> tuple[str | None, str | None]:
    parts = request["path"].strip("/").split("/")
    offset = (
        2
        if parts[:2] == ["api", "resource"]
        else (3 if parts[:3] in (["api", "v1", "resource"], ["api", "v2", "document"]) else 0)
    )
    if offset and len(parts) in {offset + 1, offset + 2}:
        return unquote(parts[offset]), unquote(parts[offset + 1]) if len(
            parts
        ) > offset + 1 else None
    return None, None


def risk_of(request: dict) -> dict:
    verb, path = request["method"], request["path"]
    dt, name = document_target(request)
    body = request.get("json_body")
    for index, arguments in enumerate((request.get("params"), body, request.get("form"))):
        if index == 0 and isinstance(arguments, list):
            arguments = dict(arguments)
        if isinstance(arguments, dict) and any(k in arguments for k in ("cmd", "run_method")):
            return {"level": "sensitive", "reason": "Dispatch override requires review"}
    if request.get("bindings"):
        return {"level": "sensitive", "reason": "Request depends on a previous response"}
    parts = path.strip("/").split("/")
    if verb in {"GET", "HEAD", "OPTIONS"} and (
        path.startswith(("/files/", "/private/files/"))
        or (
            parts[:3] == ["api", "v2", "doctype"]
            and len(parts) == 5
            and parts[-1] in {"meta", "count"}
        )
        or (parts[:3] == ["api", "v2", "document"] and len(parts) == 6 and parts[-1] == "copy")
    ):
        return {"level": "read", "reason": "Native metadata/copy/file read"}
    if dt:
        if verb in {"GET", "HEAD", "OPTIONS"}:
            return {"level": "read", "reason": "Document read", "doctype": dt, "name": name}
        reasons = []
        if verb == "DELETE":
            reasons.append("Document deletion")
        if dt.casefold() in {name.casefold() for name in SENSITIVE_DOCTYPES}:
            reasons.append("System, permission or ledger data")
        if isinstance(body, dict):
            if body.get("doctype", dt) != dt or (name and body.get("name", name) != name):
                reasons.append("Body identity differs from route identity")
            if body.get("docstatus") or body.get("workflow_state"):
                reasons.append("Submitted document or workflow change")
            if name and any(isinstance(v, list) for v in body.values()):
                reasons.append("Child-table replacement")
        elif verb != "DELETE":
            reasons.append("Body semantics are not inspectable as JSON")
        return {
            "level": "sensitive" if reasons else "write",
            "reason": "; ".join(reasons) or "Document mutation",
            "doctype": dt,
            "name": name,
        }
    method = path.split("/method/", 1)[-1] if "/method/" in path else None
    if method in READ_METHODS:
        return {"level": "read", "reason": "Known read method"}
    return {"level": "sensitive", "reason": "RPC/route side effects require review"}
