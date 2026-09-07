"""MCP entrypoints. Every mutation is prepared/executed by ExecutionEngine."""

from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Any, Literal, get_type_hints
from urllib.parse import quote

from fastmcp import FastMCP

from . import __version__, business
from .client import ERPNextError
from .engine import ExecutionEngine
from .requests import RequestSpec, ResultBinding, canonical
from .runtime import get_client, get_settings, invocation
from .state import StateStore

mcp = FastMCP(
    "ERPNext MCP",
    instructions=(
        "ERPNext API bridge with safe_mode enabled by default. No MCP DocType/method allowlist. "
        "When a tool returns confirmation_required, inspect the frozen plan, explain its target, "
        "scope and preflight findings, and ask the user once for that plan/batch. Only then call "
        "erpnext_plan_execute with the returned hash and actual user approval text. Existing "
        "explicit approval of the same scope counts; never invent consent. Confirmation is agent "
        "attestation, not independently authenticated human consent. dry_run is preflight, not "
        "a posting simulation. Read result_id/artifact_id to retrieve complete large responses. "
        "Never retry running/unknown writes without reconciling the target state."
    ),
)


def tool(*, read_only: bool = False):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            async with invocation() as (settings, _client):
                try:
                    result = await fn(*args, **kwargs)
                except ERPNextError as exc:
                    result = {"ok": False, **exc.as_dict()}
                if fn.__name__ == "erpnext_result_read":
                    return result  # Already bounded; never wrap chunks in another result artifact.
                return StateStore(settings).deliver(result)

        wrapped.__annotations__ = get_type_hints(fn)
        wrapped.__signature__ = inspect.signature(fn, eval_str=True)
        mcp.tool(
            name=fn.__name__,
            description=fn.__doc__ or fn.__name__,
            annotations={
                "readOnlyHint": read_only,
                "destructiveHint": not read_only,
                "openWorldHint": True,
                "idempotentHint": read_only,
            },
        )(wrapped)
        return wrapped

    return decorate


def engine() -> ExecutionEngine:
    return ExecutionEngine(get_settings(), get_client())


def resource(doctype: str, name: str | None = None) -> str:
    path = "/api/resource/" + quote(doctype, safe="")
    return path + "/" + quote(name, safe="") if name is not None else path


@tool(read_only=True)
async def erpnext_config_status() -> dict:
    """Inspect resolved target and safe_mode without contacting ERPNext. Legacy allowlists are inactive."""
    return {
        "ok": True,
        "version": __version__,
        **get_settings().public_status(),
        "capabilities": {
            "arbitrary_method_call": True,
            "file_operations": True,
            "api_versions": "v1/v2 routes accepted; availability is decided by the target",
            "immutable_plans": True,
            "batch_confirmation": True,
            "complete_result_artifacts": True,
            "posting_simulation": False,
        },
    }


@tool()
async def erpnext_api_request(request: RequestSpec, dry_run: bool = False) -> dict:
    """Call any same-origin ERPNext HTTP route, including API v1/v2, JSON/form/multipart/binary.

    Unknown methods become confirmable sensitive plans in safe_mode. dry_run never sends the
    requested mutation; it saves the exact request and reports what could/could not be verified.
    """
    return await engine().request(request, dry_run=dry_run)


@tool()
async def erpnext_call_method(
    method: str,
    args: dict[str, Any] | None = None,
    http_method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST",
    api_version: Literal["v1", "v2"] = "v1",
    dry_run: bool = False,
) -> dict:
    """Call any Frappe-exposed RPC method. Unlisted methods need review, not an allowlist edit."""
    if not method or any(c in method for c in "/?#\\"):
        raise ValueError(
            "method must be a dotted method name; use erpnext_api_request for other routes"
        )
    path = ("/api/method/" if api_version == "v1" else "/api/v2/method/") + method
    arguments = args or {}
    spec = RequestSpec(
        method=http_method,
        path=path,
        **(
            {
                "params": {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in arguments.items()
                }
            }
            if http_method == "GET"
            else {"json_body": arguments}
        ),
    )
    return await engine().request(spec, dry_run=dry_run)


@tool()
async def erpnext_batch_prepare(
    requests: list[RequestSpec], expected_count: int, label: str = "ERPNext batch"
) -> dict:
    """Prepare an exact ordered batch. Verify source count; one user confirmation covers the batch.

    Bind a later JSON body pointer to an earlier response using bindings, e.g.
    {"/doc": {"step": 0, "pointer": "/data"}}. Batches are not atomic and stop on error.
    """
    return await engine().prepare(requests, expected_count=expected_count, label=label)


@tool()
async def erpnext_batch_prepare_from_artifact(
    artifact_id: str, expected_count: int, label: str = "ERPNext batch"
) -> dict:
    """Prepare a complete JSON array of requests from a finalized source artifact."""
    selected = engine()
    values = json.loads(selected.store.get(artifact_id))
    if not isinstance(values, list):
        raise ValueError("Artifact must contain a JSON request array")
    return await selected.prepare(
        [RequestSpec.model_validate(v) for v in values], expected_count=expected_count, label=label
    )


@tool(read_only=True)
async def erpnext_plan_list(status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """Recover recent plan IDs after a lost tool response; list running/unknown receipts before retrying."""
    return StateStore(get_settings()).list_plans(status=status, limit=limit, offset=offset)


@tool(read_only=True)
async def erpnext_plan_inspect(plan_id: str) -> dict:
    """Read plan status, execution receipts and the full immutable request before confirming it."""
    selected = engine()
    plan = selected.store.plan(plan_id)
    result = selected.describe(plan)
    result["request_artifact"] = selected.store.put(canonical(plan["payload"]))
    return result


@tool()
async def erpnext_plan_execute(
    plan_id: str, request_sha256: str, user_confirmation: str | None = None
) -> dict:
    """Execute the frozen plan once. Supply actual in-scope user approval text for sensitive plans.

    This records agent-attested consent, not independently verified human identity. Do not
    create another plan to retry an ambiguous write. Inspect its receipt and upstream state.
    """
    return await engine().execute(plan_id, request_sha256, user_confirmation)


@tool(read_only=True)
async def erpnext_result_read(
    artifact_id: str, offset: int = 0, limit: int = 4096, json_pointer: str | None = None
) -> dict:
    """Read complete result/input bytes in bounded base64 chunks. Optional JSON pointer selects a subtree."""
    return StateStore(get_settings()).read(
        artifact_id, offset=offset, limit=limit, json_pointer=json_pointer
    )


@tool()
async def erpnext_input_begin(
    expected_bytes: int, sha256: str, mime_type: str = "application/json"
) -> dict:
    """Begin a local artifact upload using byte length and SHA-256 calculated from the source."""
    return StateStore(get_settings()).begin_upload(expected_bytes, sha256, mime_type)


@tool()
async def erpnext_input_append(artifact_id: str, offset: int, data_base64: str) -> dict:
    """Append one local input chunk (up to 256 KiB). Out-of-order or excess bytes are rejected."""
    return StateStore(get_settings()).append(artifact_id, offset, data_base64)


@tool()
async def erpnext_input_finalize(artifact_id: str) -> dict:
    """Verify complete source length and SHA-256. Only finalized inputs can be executed/uploaded."""
    return StateStore(get_settings()).finalize(artifact_id)


@tool()
async def erpnext_doc_create(
    doc: dict[str, Any], dry_run: bool = False, expected_lengths: dict[str, int] | None = None
) -> dict:
    """Create a document using native REST. safe_mode preflights; expected_lengths checks child rows."""
    dt = doc.get("doctype")
    if not isinstance(dt, str) or not dt:
        raise ValueError("doc.doctype is required")
    return await engine().request(
        RequestSpec(
            method="POST", path=resource(dt), json_body=doc, expected_lengths=expected_lengths or {}
        ),
        dry_run=dry_run,
    )


@tool()
async def erpnext_doc_update(
    doctype: str,
    name: str,
    fields: dict[str, Any],
    dry_run: bool = False,
    expected_modified: str | None = None,
    expected_lengths: dict[str, int] | None = None,
) -> dict:
    """Update native fields. In safe_mode pin modified for Frappe save conflict checks; child-table replacement is sensitive."""
    if fields.get("doctype", doctype) != doctype or fields.get("name", name) != name:
        raise ValueError("fields.doctype/name must match the requested target")
    body = dict(fields)
    if get_settings().safe_mode or dry_run or expected_modified is not None:
        current = await get_client().get_doc(doctype, name)
        actual = current.get("modified")
        if expected_modified is not None and expected_modified != actual:
            raise ValueError("Document changed since the supplied expected_modified")
        if body.get("modified") is not None and body["modified"] != actual:
            raise ValueError("fields.modified is stale")
        if actual:
            expected_modified = actual
            body["modified"] = actual
    return await engine().request(
        RequestSpec(
            method="PUT",
            path=resource(doctype, name),
            json_body=body,
            expected_modified=expected_modified,
            expected_lengths=expected_lengths or {},
        ),
        dry_run=dry_run,
    )


@tool()
async def erpnext_doc_delete(
    doctype: str, name: str, dry_run: bool = True, expected_modified: str | None = None
) -> dict:
    """Prepare/delete any document allowed by ERPNext. safe_mode requires plan confirmation; no draft-only MCP gate."""
    return await engine().request(
        RequestSpec(
            method="DELETE", path=resource(doctype, name), expected_modified=expected_modified
        ),
        dry_run=dry_run,
    )


@tool()
async def erpnext_doc_submit(doctype: str, name: str, dry_run: bool = False) -> dict:
    """Submit a frozen current document through Frappe. Sensitive: may post stock/accounting effects."""
    doc = await get_client().get_doc(doctype, name)
    return await engine().request(
        RequestSpec(method="POST", path="/api/method/frappe.client.submit", json_body={"doc": doc}),
        dry_run=dry_run,
    )


@tool()
async def erpnext_doc_cancel(doctype: str, name: str, dry_run: bool = False) -> dict:
    """Cancel through Frappe. Sensitive; preflight cannot simulate ledger reversal or custom hooks."""
    return await engine().request(
        RequestSpec(
            method="POST",
            path="/api/method/frappe.client.cancel",
            json_body={"doctype": doctype, "name": name},
        ),
        dry_run=dry_run,
    )


@tool(read_only=True)
async def erpnext_doc_list(
    doctype: str,
    fields: list[str] | None = None,
    filters: dict[str, Any] | list[Any] | None = None,
    or_filters: list[Any] | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_total: bool = False,
) -> dict:
    """List one page without a hidden limit clamp. has_more is unknown for a full page without total."""
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset non-negative")
    if include_total and or_filters:
        raise ValueError("Use native v2 count/raw API for totals with or_filters")
    client = get_client()
    rows = await client.list_docs(
        doctype,
        fields=fields,
        filters=filters,
        or_filters=or_filters,
        order_by=order_by or "name asc",
        limit=limit,
        offset=offset,
    )
    total = await client.get_count(doctype, filters=filters) if include_total else None
    more = (
        offset + len(rows) < total if total is not None else (None if len(rows) >= limit else False)
    )
    return {
        "doctype": doctype,
        "count": len(rows),
        "page_count": len(rows),
        "requested_limit": limit,
        "offset": offset,
        "total_count": total,
        "has_more": more,
        "next_offset": offset + len(rows) if more is not False else None,
        "complete": offset == 0 and more is False,
        "data": rows,
        "consistency": "Live pagination; concurrent changes may alter later pages",
    }


@tool(read_only=True)
async def erpnext_doc_get(doctype: str, name: str) -> dict:
    """Read a complete document; large documents are returned through result artifacts."""
    return await get_client().get_doc(doctype, name)


@tool()
async def erpnext_doc_map(
    source_doctype: str,
    source_name: str,
    target_doctype: str,
    save_draft: bool = False,
    extra_args: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict:
    """Generate a common mapped document, then freeze any requested save. Use call_method for other mappings."""
    method = business.DOC_MAP_METHODS.get((source_doctype, target_doctype))
    if not method:
        raise ValueError(
            "No convenience mapping; use erpnext_call_method with the target's mapping method"
        )
    if {"cmd", "run_method", "source_name"}.intersection(extra_args or {}):
        raise ValueError(
            "extra_args cannot replace dispatch or source identity; use erpnext_call_method"
        )
    args = {"source_name": source_name, **(extra_args or {})}
    generated = (await get_client().call_method(method, args)).get("message", {})
    if not save_draft:
        return {"saved": False, "doc": generated}
    return await engine().request(
        RequestSpec(method="POST", path=resource(target_doctype), json_body=generated),
        dry_run=dry_run,
    )


@tool()
async def erpnext_bom_create_revision(
    source_bom: str, fields: dict[str, Any] | None = None, dry_run: bool = False
) -> dict:
    """Generate and freeze a draft revision of a submitted BOM; source remains unchanged."""
    overrides = fields or {}
    if {"doctype", "name", "docstatus", "amended_from"}.intersection(overrides):
        raise ValueError("Revision overrides cannot change identity/docstatus/amended_from")
    client = get_client()
    source = await client.get_doc("BOM", source_bom)
    if source.get("docstatus") != 1:
        raise ValueError("source_bom must be submitted")
    doc = await business._copy_writable_document("BOM", source, client=client, schema_cache={})
    doc.update(overrides)
    if "routing" in overrides and "operations" not in overrides:
        doc.pop("operations", None)
    doc["doctype"] = "BOM"
    return await engine().request(
        RequestSpec(method="POST", path=resource("BOM"), json_body=doc), dry_run=dry_run
    )


@tool()
async def erpnext_work_order_make_stock_entry(
    work_order: str,
    purpose: str,
    qty: float | None = None,
    target_warehouse: str | None = None,
    is_additional_transfer_entry: bool = False,
    source_stock_entry: str | None = None,
    save_draft: bool = False,
    submit: bool = False,
    dry_run: bool = False,
) -> dict:
    """Generate Stock Entry using ERPNext; freeze create/submit as one confirmable dependent batch."""
    client = get_client()
    generated = await client.make_work_order_stock_entry(
        work_order,
        purpose,
        qty=qty,
        target_warehouse=target_warehouse,
        is_additional_transfer_entry=is_additional_transfer_entry,
        source_stock_entry=source_stock_entry,
    )
    if not (save_draft or submit):
        checks = await business._validate_doc_payload(
            "Stock Entry", generated, client=client, settings=get_settings()
        )
        return {"saved": False, "submitted": False, "doc": generated, "validation": checks}
    selected = engine()
    specs = [RequestSpec(method="POST", path=resource("Stock Entry"), json_body=generated)]
    if not submit:
        return await selected.request(specs[0], dry_run=dry_run)
    specs.append(
        RequestSpec(
            method="POST",
            path="/api/method/frappe.client.submit",
            json_body={"doc": None},
            bindings={"/doc": ResultBinding(step=0, pointer="/data")},
        )
    )
    prepared = await selected.prepare(
        specs, expected_count=2, label=f"Create and submit Stock Entry for {work_order}"
    )
    if dry_run or prepared["confirmation_required"]:
        return prepared
    return await selected.execute(prepared["plan_id"], prepared["request_sha256"])


@tool()
async def erpnext_naming_series_configure(
    doctype: str,
    series_options: list[str],
    counters: dict[str, int] | None = None,
    dry_run: bool = True,
) -> dict:
    """Freeze native naming-series controller requests as one sensitive plan; no separate whitelist."""
    if (
        not series_options
        or any(not s.strip() for s in series_options)
        or len(set(series_options)) != len(series_options)
    ):
        raise ValueError("Provide nonempty, unique naming series")
    counters = counters or {}
    if set(counters) - set(series_options):
        raise ValueError("Counter series is not present in series_options")
    if any(isinstance(v, bool) or v < 0 for v in counters.values()):
        raise ValueError("Counters must be non-negative integers")
    current = await get_client().get_doc("Document Naming Settings", "Document Naming Settings")

    def make(method, fields):
        return RequestSpec(
            method="POST",
            path="/api/method/frappe.handler.run_doc_method",
            json_body={"method": method, "docs": json.dumps({**current, **fields}), "args": "{}"},
        )

    specs = [
        make(
            "update_series",
            {
                "transaction_type": doctype,
                "naming_series_options": "\n".join(series_options),
                "user_must_always_select": 0,
            },
        )
    ]
    specs.extend(
        make("update_series_start", {"prefix": key, "current_value": value})
        for key, value in counters.items()
    )
    selected = engine()
    prepared = await selected.prepare(
        specs, expected_count=len(specs), label=f"Configure {doctype} numbering"
    )
    if dry_run or prepared["confirmation_required"]:
        return prepared
    return await selected.execute(prepared["plan_id"], prepared["request_sha256"])


# Specialized read/preflight conveniences retain the original business knowledge. Their writes
# were removed during extraction; only the functions below are registered from this module.
for _name in (
    "erpnext_health_check",
    "erpnext_doctype_schema",
    "erpnext_bom_preflight",
    "erpnext_link_search",
    "erpnext_report_run",
    "erpnext_stock_balance",
    "erpnext_process_trace",
    "erpnext_party_balance",
):
    globals()[_name] = tool(read_only=True)(getattr(business, _name))


def main() -> None:
    settings = get_settings()
    if settings.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False, log_level="WARNING")
    else:
        mcp.run(
            transport=settings.transport,
            host=settings.host,
            port=settings.port,
            show_banner=False,
            log_level="INFO",
        )


if __name__ == "__main__":
    main()
