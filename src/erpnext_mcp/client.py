from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


class ERPNextError(RuntimeError):
    """Raised when ERPNext/Frappe returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        exc_type: str | None = None,
        exception: str | None = None,
        server_messages: list[dict[str, Any]] | None = None,
        transaction_rolled_back: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.exc_type = exc_type
        self.exception = exception
        self.server_messages = server_messages or []
        self.transaction_rolled_back = transaction_rolled_back

    @property
    def fatal_messages(self) -> list[dict[str, Any]]:
        fatal = [message for message in self.server_messages if message.get("is_fatal")]
        if fatal:
            return fatal
        return [{"message": str(self), "is_fatal": True}]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [message for message in self.server_messages if not message.get("is_fatal")]

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "http_status": self.status_code,
            "exception_type": self.exc_type,
            "exception": self.exception,
            "warnings": self.warnings,
            "errors": self.fatal_messages,
            "server_messages": self.server_messages,
            "transaction_rolled_back": self.transaction_rolled_back,
        }


class ERPNextClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.erpnext_url,
            headers=self._headers,
            timeout=settings.timeout,
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
                keepalive_expiry=settings.keepalive_expiry,
            ),
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            **({"X-Frappe-Site-Name": self.settings.site} if self.settings.site else {}),
            "Authorization": (
                f"Bearer {self.settings.access_token}"
                if self.settings.access_token
                else f"token {self.settings.api_key}:{self.settings.api_secret}"
            ),
            "Accept": "application/json",
        }

    async def send(self, request: dict, store) -> dict:
        """Perform one exact prepared request; preserve JSON, text, files and status.

        Redirects are returned, never automatically followed with ERPNext credentials.
        This method is intentionally policy-free; the execution engine owns policy.
        """
        kwargs: dict[str, Any] = {
            "params": request.get("params"),
            "headers": request.get("headers") or {},
        }
        if request.get("json_provided") or request.get("json_body") is not None:
            kwargs["content"] = json.dumps(
                request["json_body"], ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if not any(k.lower() == "content-type" for k in kwargs["headers"]):
                kwargs["headers"]["Content-Type"] = "application/json"
        elif request.get("body_base64") is not None:
            kwargs["content"] = base64.b64decode(request["body_base64"], validate=True)
        elif request.get("files"):
            kwargs["data"] = request.get("form") or {}
            kwargs["files"] = [
                (
                    part["field"],
                    (part["filename"], store.get(part["artifact_id"]), part["content_type"]),
                )
                for part in request["files"]
            ]
        elif request.get("form") is not None:
            kwargs["data"] = request["form"]
        async with self._client.stream(
            request["method"], request["path"], follow_redirects=False, **kwargs
        ) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.settings.max_artifact_bytes:
                    raise ERPNextError(
                        "Response exceeds configured complete-artifact limit",
                        status_code=response.status_code,
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
            mime = response.headers.get("content-type", "application/octet-stream")
            artifact = store.put(raw, mime)
            headers = {k: v for k, v in response.headers.items() if k.lower() not in {"set-cookie"}}
            result = {
                "ok": response.is_success,
                "http_status": response.status_code,
                "headers": headers,
                "body": artifact,
                "transport_complete": True,
                "transaction_rolled_back": None,
            }
            try:

                def reject_constant(value):
                    raise ValueError(f"Non-JSON constant: {value}")

                payload = json.loads(raw, parse_constant=reject_constant)
                json_decoded = True
            except (ValueError, UnicodeDecodeError):
                payload = None
                json_decoded = False
            result["body_format"] = (
                "json" if json_decoded else ("text" if mime.startswith("text/") else "binary")
            )
            if json_decoded:
                if isinstance(payload, dict) and ("exception" in payload or "exc_type" in payload):
                    result["ok"] = False
                if len(raw) <= self.settings.inline_result_bytes:
                    result["data"] = payload
            elif mime.startswith("text/") and len(raw) <= self.settings.inline_result_bytes:
                result["text"] = raw.decode(response.encoding or "utf-8", errors="replace")
            return result

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, params=params, json=json_body)

        if response.is_error:
            raise _build_response_error(response)

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ERPNextError(
                f"ERPNext returned non-JSON response: {response.text[:300]}"
            ) from exc

        if isinstance(payload, dict) and ("exception" in payload or "exc_type" in payload):
            raise _build_payload_error(
                payload,
                status_code=response.status_code,
                transaction_rolled_back=None,
            )
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call_method(
        self, method_name: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("POST", f"/api/method/{method_name}", json_body=args or {})

    async def get_logged_user(self) -> str:
        payload = await self.call_method("frappe.auth.get_logged_user")
        return payload.get("message", "")

    async def get_versions(self) -> dict[str, Any]:
        payload = await self.call_method("frappe.utils.change_log.get_versions")
        return payload.get("message", {})

    async def get_doctype(self, doctype: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/api/resource/DocType/{_q(doctype)}")
        return payload.get("data", {})

    async def get_custom_fields(self, doctype: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            page = await self.list_docs(
                "Custom Field",
                fields=[
                    "fieldname",
                    "label",
                    "fieldtype",
                    "options",
                    "reqd",
                    "read_only",
                    "hidden",
                    "in_list_view",
                    "in_standard_filter",
                    "default",
                    "depends_on",
                    "mandatory_depends_on",
                    "precision",
                    "length",
                    "description",
                    "idx",
                ],
                filters={"dt": doctype},
                order_by="idx asc",
                limit=500,
                offset=len(rows),
            )
            rows.extend(page)
            if len(page) < 500:
                return rows

    async def list_docs(
        self,
        doctype: str,
        *,
        fields: list[str] | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        or_filters: list[Any] | None = None,
        order_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        params: dict[str, Any] = {
            "limit_page_length": limit,
            "limit_start": offset,
        }
        if fields:
            params["fields"] = json.dumps(fields)
        if filters:
            params["filters"] = json.dumps(filters)
        if or_filters:
            params["or_filters"] = json.dumps(or_filters)
        if order_by:
            params["order_by"] = order_by

        payload = await self.request("GET", f"/api/resource/{_q(doctype)}", params=params)
        return payload.get("data", [])

    async def get_count(
        self,
        doctype: str,
        *,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> int:
        params: dict[str, Any] = {"doctype": doctype}
        if filters:
            params["filters"] = json.dumps(filters)
        payload = await self.request("GET", "/api/method/frappe.client.get_count", params=params)
        return int(payload.get("message", 0))

    async def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/api/resource/{_q(doctype)}/{_q(name)}")
        return payload.get("data", {})

    async def doc_exists(self, doctype: str, name: str) -> bool:
        payload = await self.call_method(
            "frappe.client.get_value",
            {"doctype": doctype, "fieldname": "name", "filters": {"name": name}},
        )
        return bool(payload.get("message"))

    async def search_link(
        self,
        doctype: str,
        *,
        text: str = "",
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        payload = await self.call_method(
            "frappe.desk.search.search_link",
            {
                "doctype": doctype,
                "txt": text,
                "filters": filters or {},
                "page_length": max(1, min(limit, 100)),
            },
        )
        return payload.get("message", [])

    async def run_report(
        self, report_name: str, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = await self.request(
            "GET",
            "/api/method/frappe.desk.query_report.run",
            params={
                "report_name": report_name,
                "filters": json.dumps(filters or {}),
                "ignore_prepared_report": 1,
            },
        )
        return payload.get("message", {})

    async def get_bin_details(
        self,
        item_code: str,
        warehouse: str,
        *,
        company: str | None = None,
        include_child_warehouses: bool = False,
    ) -> dict[str, Any]:
        payload = await self.call_method(
            "erpnext.stock.get_item_details.get_bin_details",
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "company": company,
                "include_child_warehouses": int(include_child_warehouses),
            },
        )
        return payload.get("message", {})

    async def make_work_order_stock_entry(
        self,
        work_order: str,
        purpose: str,
        *,
        qty: float | None = None,
        target_warehouse: str | None = None,
        is_additional_transfer_entry: bool = False,
        source_stock_entry: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "work_order_id": work_order,
            "purpose": purpose,
            "is_additional_transfer_entry": is_additional_transfer_entry,
        }
        if qty is not None:
            args["qty"] = qty
        if target_warehouse:
            args["target_warehouse"] = target_warehouse
        if source_stock_entry:
            args["source_stock_entry"] = source_stock_entry

        payload = await self.call_method(
            "erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry",
            args,
        )
        return payload.get("message", {})

    async def get_party_balance(
        self,
        party_type: str,
        party: str,
        *,
        company: str | None = None,
        date: str | None = None,
    ) -> float | int:
        payload = await self.call_method(
            "erpnext.accounts.utils.get_balance_on",
            {
                "party_type": party_type,
                "party": party,
                "company": company,
                "date": date,
            },
        )
        return payload.get("message", 0)


def _q(value: str) -> str:
    return quote(value, safe="")


def _extract_error(response: httpx.Response) -> str:
    return str(_build_response_error(response))


def _build_response_error(response: httpx.Response) -> ERPNextError:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        message = f"ERPNext HTTP {response.status_code}: {response.text[:500]}"
        return ERPNextError(
            message,
            status_code=response.status_code,
            transaction_rolled_back=None,
        )
    return _build_payload_error(
        payload,
        status_code=response.status_code,
        transaction_rolled_back=None,
    )


def _extract_payload_error(payload: dict[str, Any]) -> str:
    return str(_build_payload_error(payload))


def _build_payload_error(
    payload: dict[str, Any],
    *,
    status_code: int | None = None,
    transaction_rolled_back: bool | None = None,
) -> ERPNextError:
    messages = decode_server_messages(payload.get("_server_messages"))
    fatal_messages = [message for message in messages if message.get("is_fatal")]

    exception = payload.get("exception")
    exc_type = payload.get("exc_type")
    message = "; ".join(str(item.get("message")) for item in fatal_messages)
    if not message:
        message = str(exception or exc_type or payload.get("message") or "Unknown ERPNext error")

    return ERPNextError(
        message,
        status_code=status_code,
        exc_type=str(exc_type) if exc_type else None,
        exception=str(exception) if exception else None,
        server_messages=messages,
        transaction_rolled_back=transaction_rolled_back,
    )


def decode_server_messages(value: Any) -> list[dict[str, Any]]:
    """Decode Frappe's nested JSON server-message format without losing metadata."""
    if not value:
        return []

    decoded: Any = value
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return [{"message": decoded, "is_fatal": False}]

    if not isinstance(decoded, list):
        decoded = [decoded]

    messages: list[dict[str, Any]] = []
    for item in decoded:
        parsed: Any = item
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                parsed = {"message": parsed}
        if not isinstance(parsed, dict):
            parsed = {"message": str(parsed)}

        normalized = dict(parsed)
        normalized["message"] = str(normalized.get("message") or parsed)
        normalized["is_fatal"] = bool(normalized.get("raise_exception"))
        messages.append(normalized)
    return messages
