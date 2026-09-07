from __future__ import annotations


import asyncio


from collections import OrderedDict


from copy import deepcopy


from time import monotonic


from typing import Any, Literal


from .client import ERPNextClient, ERPNextError


from .config import Settings
from .runtime import get_client, get_settings


SYSTEM_FIELDS = {
    "__islocal",
    "__unsaved",
    "_assign",
    "_comments",
    "_liked_by",
    "_seen",
    "amended_from",
    "creation",
    "docstatus",
    "doctype",
    "idx",
    "lft",
    "modified",
    "modified_by",
    "name",
    "old_parent",
    "owner",
    "parent",
    "parentfield",
    "parenttype",
    "rgt",
}


DOC_MAP_METHODS: dict[tuple[str, str], str] = {
    (
        "Sales Order",
        "Sales Invoice",
    ): "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
    (
        "Sales Order",
        "Delivery Note",
    ): "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
    (
        "Purchase Order",
        "Purchase Invoice",
    ): "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
    (
        "Purchase Order",
        "Purchase Receipt",
    ): "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
    (
        "Sales Invoice",
        "Delivery Note",
    ): "erpnext.accounts.doctype.sales_invoice.sales_invoice.make_delivery_note",
}


WORK_ORDER_STOCK_ENTRY_PURPOSES = {
    "Material Transfer for Manufacture",
    "Manufacture",
    "Material Consumption for Manufacture",
    "Disassemble",
}


NAMING_SERIES_DOCTYPES = {"Sales Order", "Work Order"}


TRACE_DOCTYPE_FIELDS: dict[str, list[str]] = {
    "Sales Order": [
        "name",
        "docstatus",
        "status",
        "customer",
        "company",
        "transaction_date",
        "grand_total",
        "base_grand_total",
    ],
    "Sales Order Item": ["parent", "item_code", "qty", "delivered_qty", "amount"],
    "Delivery Note": ["name", "docstatus", "status", "customer", "company", "posting_date"],
    "Delivery Note Item": ["parent", "against_sales_order", "item_code", "qty", "amount"],
    "Purchase Order": [
        "name",
        "docstatus",
        "status",
        "supplier",
        "company",
        "transaction_date",
        "grand_total",
    ],
    "Purchase Order Item": ["parent", "item_code", "qty", "received_qty", "amount"],
    "Purchase Receipt": ["name", "docstatus", "status", "supplier", "company", "posting_date"],
    "Purchase Receipt Item": ["parent", "purchase_order", "item_code", "qty", "amount"],
    "Subcontracting Order": [
        "name",
        "docstatus",
        "status",
        "purchase_order",
        "supplier",
        "company",
        "transaction_date",
    ],
    "Subcontracting Receipt": [
        "name",
        "docstatus",
        "status",
        "supplier",
        "company",
        "posting_date",
    ],
    "Production Plan": ["name", "docstatus", "status", "company", "posting_date"],
    "Production Plan Item": [
        "parent",
        "item_code",
        "bom_no",
        "planned_qty",
        "ordered_qty",
        "planned_start_date",
    ],
    "Work Order": [
        "name",
        "docstatus",
        "status",
        "company",
        "production_item",
        "bom_no",
        "qty",
        "produced_qty",
        "sales_order",
        "production_plan",
        "planned_start_date",
    ],
    "Work Order Operation": [
        "parent",
        "operation",
        "workstation",
        "status",
        "completed_qty",
        "time_in_mins",
    ],
    "Job Card": [
        "name",
        "docstatus",
        "status",
        "work_order",
        "operation",
        "workstation",
        "total_completed_qty",
        "for_quantity",
    ],
    "Stock Entry": [
        "name",
        "docstatus",
        "purpose",
        "stock_entry_type",
        "company",
        "posting_date",
        "work_order",
        "total_incoming_value",
        "total_outgoing_value",
        "value_difference",
    ],
    "Stock Entry Detail": [
        "parent",
        "item_code",
        "qty",
        "s_warehouse",
        "t_warehouse",
        "basic_rate",
        "amount",
    ],
    "Quality Inspection": [
        "name",
        "docstatus",
        "status",
        "inspection_type",
        "reference_type",
        "reference_name",
        "item_code",
        "report_date",
    ],
    "Stock Ledger Entry": [
        "name",
        "posting_date",
        "voucher_type",
        "voucher_no",
        "item_code",
        "warehouse",
        "actual_qty",
        "stock_value_difference",
    ],
    "GL Entry": [
        "name",
        "posting_date",
        "voucher_type",
        "voucher_no",
        "account",
        "debit",
        "credit",
    ],
}


TRACE_DOCTYPES = tuple(TRACE_DOCTYPE_FIELDS)


class _MetadataCache:
    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )

    def get(self, settings: Settings, doctype: str) -> dict[str, Any] | None:
        if settings.metadata_cache_ttl <= 0:
            return None
        key = self._key(settings, doctype)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= monotonic():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return deepcopy(value)

    def put(self, settings: Settings, doctype: str, value: dict[str, Any]) -> None:
        if settings.metadata_cache_ttl <= 0:
            return
        key = self._key(settings, doctype)
        self._entries[key] = (monotonic() + settings.metadata_cache_ttl, deepcopy(value))
        self._entries.move_to_end(key)
        while len(self._entries) > settings.metadata_cache_max_entries:
            self._entries.popitem(last=False)

    def clear_target(self, settings: Settings) -> None:
        target = (settings.erpnext_url, settings.identity)
        for key in [key for key in self._entries if key[:2] == target]:
            self._entries.pop(key, None)

    def public_status(self) -> dict[str, int]:
        return {"entries": len(self._entries)}

    @staticmethod
    def _key(settings: Settings, doctype: str) -> tuple[str, str, str]:
        return settings.erpnext_url, settings.identity, doctype


_METADATA_CACHE = _MetadataCache()


def _select_options(field: dict[str, Any]) -> list[str]:
    if field.get("fieldtype") != "Select" or not field.get("options"):
        return []
    return [item for item in str(field["options"]).splitlines() if item]


def _compact_field(field: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "fieldname": field.get("fieldname"),
        "label": field.get("label"),
        "fieldtype": field.get("fieldtype"),
        "options": field.get("options"),
        "select_options": _select_options(field),
        "reqd": bool(field.get("reqd")),
        "read_only": bool(field.get("read_only")),
        "hidden": bool(field.get("hidden")),
        "in_list_view": bool(field.get("in_list_view")),
        "in_standard_filter": bool(field.get("in_standard_filter")),
        "default": field.get("default"),
        "depends_on": field.get("depends_on"),
        "mandatory_depends_on": field.get("mandatory_depends_on"),
        "precision": field.get("precision"),
        "length": field.get("length"),
        "description": field.get("description"),
    }
    return compact


def _build_doctype_schema(
    doc: dict[str, Any], custom_fields: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    merged_fields: dict[str, dict[str, Any]] = {
        str(field["fieldname"]): field for field in doc.get("fields", []) if field.get("fieldname")
    }
    for field in custom_fields or []:
        if field.get("fieldname"):
            merged_fields[str(field["fieldname"])] = field

    fields = [_compact_field(field) for field in merged_fields.values()]
    return {
        "name": doc.get("name"),
        "module": doc.get("module"),
        "is_submittable": bool(doc.get("is_submittable")),
        "is_single": bool(doc.get("issingle")),
        "is_tree": bool(doc.get("is_tree")),
        "required_fields": [
            field["fieldname"] for field in fields if field["reqd"] and not field["read_only"]
        ],
        "link_fields": [
            field
            for field in fields
            if field["fieldtype"] in {"Link", "Dynamic Link"} and field["options"]
        ],
        "select_fields": [field for field in fields if field["fieldtype"] == "Select"],
        "table_fields": [
            field for field in fields if field["fieldtype"] in {"Table", "Table MultiSelect"}
        ],
        "fields": fields,
    }


def _new_validation() -> dict[str, Any]:
    return {
        "schema_valid": True,
        "erpnext_business_valid": True,
        "known_valid": True,
        "validation_complete": True,
        "submit_ready": True,
        "missing_required_fields": [],
        "unknown_link_values": [],
        "invalid_child_tables": [],
        "invalid_select_values": [],
        "unknown_fields": [],
        "business_rule_errors": [],
        "submit_readiness_errors": [],
        "business_warnings": [],
        "compatibility_errors": [],
        "business_context": {},
        "warnings": [],
    }


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _join_path(path: str, fieldname: str) -> str:
    return f"{path}.{fieldname}" if path else fieldname


def _bounded_limit(limit: int, maximum: int = 100) -> int:
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}; use doc_list for larger pages")
    return limit


def _doc_summary(doc: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "doctype",
        "name",
        "docstatus",
        "status",
        "workflow_state",
        "company",
        "posting_date",
        "transaction_date",
        "item",
        "item_code",
        "qty",
        "stock_uom",
        "production_item",
        "sales_order",
        "production_plan",
        "purchase_order",
        "supplier",
        "reference_type",
        "reference_name",
        "inspection_type",
        "operation",
        "workstation",
        "bom_no",
        "work_order",
        "purpose",
        "stock_entry_type",
        "from_warehouse",
        "to_warehouse",
        "fg_completed_qty",
        "total_qty",
        "total_cost",
        "raw_material_cost",
        "operating_cost",
        "total_operating_cost",
        "grand_total",
        "base_grand_total",
        "total_incoming_value",
        "total_outgoing_value",
        "value_difference",
        "material_transferred_for_manufacturing",
        "produced_qty",
    ]
    summary = {key: doc.get(key) for key in keys if doc.get(key) not in (None, "", [])}
    items = doc.get("items")
    if isinstance(items, list):
        summary["item_rows"] = len(items)
    operations = doc.get("operations")
    if isinstance(operations, list):
        summary["operation_rows"] = len(operations)
    return summary


def _document_response(
    doc: dict[str, Any],
    *,
    include_doc: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {"summary": _doc_summary(doc)}
    if extra:
        response.update(extra)
    if include_doc:
        response["doc"] = doc
    return response


async def _copy_writable_document(
    doctype: str,
    doc: dict[str, Any],
    *,
    client: ERPNextClient,
    schema_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schema = await _schema_for_validation(doctype, client=client, schema_cache=schema_cache)
    copied: dict[str, Any] = {"doctype": doctype}
    for field in schema["fields"]:
        fieldname = field["fieldname"]
        if field["read_only"] or fieldname not in doc:
            continue
        value = doc[fieldname]
        if field["fieldtype"] not in {"Table", "Table MultiSelect"}:
            copied[fieldname] = value
            continue

        child_doctype = str(field["options"])
        child_rows: list[dict[str, Any]] = []
        if isinstance(value, list):
            for row in value:
                if not isinstance(row, dict):
                    continue
                child_rows.append(
                    await _copy_writable_document(
                        child_doctype,
                        row,
                        client=client,
                        schema_cache=schema_cache,
                    )
                )
        copied[fieldname] = child_rows
    return copied


def _normalize_link_row(row: Any, *, matched_on: str) -> dict[str, Any]:
    if isinstance(row, dict):
        normalized = dict(row)
        value = normalized.get("value") or normalized.get("name")
        if value is not None:
            normalized["value"] = value
        normalized.setdefault("label", normalized.get("description"))
        normalized.setdefault("matched_on", matched_on)
        return normalized

    if isinstance(row, (list, tuple)):
        normalized = {"value": row[0] if row else None, "matched_on": matched_on}
        if len(row) > 1:
            normalized["label"] = row[1]
        if len(row) > 2:
            normalized["description"] = row[2]
        return normalized

    return {"value": row, "matched_on": matched_on}


def _link_value(row: dict[str, Any]) -> str:
    return str(row.get("value") or row.get("name") or "")


def _dedupe_link_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        value = _link_value(row)
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(row)
    return deduped


def _append_filter(filters: list[list[Any]], fieldname: str, operator: str, value: Any) -> None:
    if value not in (None, "", []):
        filters.append([fieldname, operator, value])


def _trace_name_filters(tag: str | None, explicit_name: str | None = None) -> list[list[Any]]:
    filters: list[list[Any]] = []
    if explicit_name:
        filters.append(["name", "=", explicit_name])
    elif tag:
        filters.append(["name", "like", f"%{tag}%"])
    return filters


async def _safe_trace_list(
    client: ERPNextClient,
    doctype: str,
    *,
    fields: list[str],
    filters: list[list[Any]] | None = None,
    or_filters: list[list[Any]] | None = None,
    order_by: str | None = "modified desc",
    limit: int = 20,
    require_filters: bool = False,
) -> dict[str, Any]:
    query = {
        "filters": filters or None,
        "or_filters": or_filters or None,
        "order_by": order_by,
        "limit": limit,
    }
    if require_filters and not filters and not or_filters:
        return {
            "ok": True,
            "doctype": doctype,
            "count": 0,
            "skipped": True,
            "skip_reason": "No specific trace anchor for this section.",
            "query": query,
            "data": [],
        }
    try:
        data = await client.list_docs(
            doctype,
            fields=fields,
            filters=filters or None,
            or_filters=or_filters or None,
            order_by=order_by,
            limit=limit,
        )
        return {
            "ok": True,
            "doctype": doctype,
            "count": len(data),
            "query": query,
            "has_more": None if len(data) >= limit else False,
            "complete": len(data) < limit,
            "next_offset": len(data) if len(data) >= limit else None,
            "data": data,
        }
    except ERPNextError as exc:
        return {
            "ok": False,
            "doctype": doctype,
            "error": str(exc),
            "query": query,
            "data": [],
        }


async def _safe_trace_get(
    client: ERPNextClient,
    doctype: str,
    name: str | None,
) -> dict[str, Any] | None:
    if not name:
        return None
    try:
        return await client.get_doc(doctype, name)
    except ERPNextError:
        return None


async def _validate_doc_payload(
    doctype: str,
    doc: dict[str, Any],
    *,
    client: ERPNextClient,
    settings: Settings,
) -> dict[str, Any]:
    validation = _new_validation()
    schema_cache: dict[str, dict[str, Any]] = {}
    exists_cache: dict[tuple[str, str], bool] = {}
    await _validate_doc_payload_into(
        doctype,
        doc,
        client=client,
        settings=settings,
        validation=validation,
        schema_cache=schema_cache,
        exists_cache=exists_cache,
        path="$",
    )
    await _validate_known_business_rules(doctype, doc, client=client, validation=validation)
    validation["schema_valid"] = not any(
        validation[key]
        for key in (
            "missing_required_fields",
            "unknown_link_values",
            "invalid_child_tables",
            "invalid_select_values",
            "unknown_fields",
        )
    )
    validation["erpnext_business_valid"] = not validation["business_rule_errors"]
    validation["known_valid"] = validation["schema_valid"] and validation["erpnext_business_valid"]
    validation["validation_complete"] = True
    validation["business_execution_guaranteed"] = False
    validation["valid"] = validation["known_valid"] and validation["validation_complete"]
    validation["submit_ready"] = validation["valid"] and not validation["submit_readiness_errors"]
    if doctype == "Routing":
        validation["bom_compatible"] = not validation["compatibility_errors"]
    return validation


def _numeric_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _checked(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"1", "true", "yes"}


def _rule_issue(code: str, path: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "path": path, "message": message, **extra}


async def _validate_known_business_rules(
    doctype: str,
    doc: dict[str, Any],
    *,
    client: ERPNextClient,
    validation: dict[str, Any],
) -> None:
    if doctype == "BOM":
        await _validate_bom_business_rules(doc, client=client, validation=validation)
    elif doctype == "Routing":
        _validate_routing_business_rules(doc, validation=validation)


async def _validate_bom_business_rules(
    doc: dict[str, Any],
    *,
    client: ERPNextClient,
    validation: dict[str, Any],
) -> None:
    errors = validation["business_rule_errors"]
    submit_errors = validation["submit_readiness_errors"]

    if _numeric_value(doc.get("quantity")) <= 0:
        errors.append(
            _rule_issue(
                "bom_quantity_not_positive",
                "$.quantity",
                "BOM quantity must be greater than 0.",
            )
        )

    items = doc.get("items") if isinstance(doc.get("items"), list) else []
    for index, row in enumerate(items):
        if isinstance(row, dict) and _numeric_value(row.get("qty")) <= 0:
            errors.append(
                _rule_issue(
                    "bom_item_quantity_not_positive",
                    f"$.items[{index}].qty",
                    "BOM item quantity must be greater than 0.",
                    item_code=row.get("item_code"),
                )
            )

    if not _checked(doc.get("with_operations")):
        return

    operations = doc.get("operations") if isinstance(doc.get("operations"), list) else []
    operation_source = "payload"
    routing_name = doc.get("routing")
    if not operations and isinstance(routing_name, str) and routing_name:
        try:
            routing = await client.get_doc("Routing", routing_name)
        except ERPNextError as exc:
            validation["business_warnings"].append(
                _rule_issue(
                    "routing_business_check_unavailable",
                    "$.routing",
                    f"Could not load Routing for business-rule validation: {exc}",
                    routing=routing_name,
                )
            )
        else:
            operations = (
                routing.get("operations") if isinstance(routing.get("operations"), list) else []
            )
            operation_source = f"routing:{routing_name}"

    validation["business_context"]["operation_source"] = operation_source
    validation["business_context"]["resolved_operation_rows"] = len(operations)

    if not operations:
        submit_errors.append(
            _rule_issue(
                "bom_operations_missing",
                "$.operations",
                "A BOM with operations must contain at least one operation before submission.",
            )
        )
    else:
        _validate_operation_rows(
            operations,
            path="$.operations",
            issues=errors,
            compatibility_only=False,
        )

    if _checked(doc.get("track_semi_finished_goods")):
        finished_goods = [
            row.get("finished_good")
            for row in operations
            if isinstance(row, dict) and row.get("finished_good")
        ]
        if not finished_goods:
            errors.append(
                _rule_issue(
                    "semi_finished_final_operation_missing",
                    "$.operations",
                    "Track Semi Finished Goods requires an operation with a finished-good item.",
                )
            )
        elif len(finished_goods) > 1:
            errors.append(
                _rule_issue(
                    "multiple_final_finished_goods",
                    "$.operations",
                    "Only one operation can produce the final finished good.",
                )
            )

    unresolved_rate_items = [
        str(row.get("item_code"))
        for row in items
        if isinstance(row, dict)
        and row.get("item_code")
        and not row.get("bom_no")
        and not _checked(row.get("sourced_by_supplier"))
        and _numeric_value(row.get("rate")) <= 0
    ]
    if unresolved_rate_items:
        validation["business_warnings"].append(
            _rule_issue(
                "material_rate_resolution_deferred",
                "$.items",
                "ERPNext will resolve zero material rates during save and may emit valuation or price warnings.",
                rate_basis=doc.get("rm_cost_as_per") or "Valuation Rate",
                item_codes=unresolved_rate_items,
            )
        )


def _validate_routing_business_rules(doc: dict[str, Any], *, validation: dict[str, Any]) -> None:
    operations = doc.get("operations") if isinstance(doc.get("operations"), list) else []
    compatibility_errors = validation["compatibility_errors"]
    if not operations:
        compatibility_errors.append(
            _rule_issue(
                "routing_operations_missing",
                "$.operations",
                "An empty Routing can be saved but cannot populate an operational BOM.",
            )
        )
        return

    _validate_operation_rows(
        operations,
        path="$.operations",
        issues=compatibility_errors,
        compatibility_only=True,
    )


def _validate_operation_rows(
    rows: list[Any],
    *,
    path: str,
    issues: list[dict[str, Any]],
    compatibility_only: bool,
) -> None:
    prefix = "routing_bom_compatibility" if compatibility_only else "bom_operation"
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_path = f"{path}[{index}]"
        operation = row.get("operation")
        if not row.get("workstation") and not row.get("workstation_type"):
            issues.append(
                _rule_issue(
                    f"{prefix}_workstation_missing",
                    row_path,
                    "Workstation or Workstation Type is required for a BOM operation.",
                    operation=operation,
                )
            )
        if _numeric_value(row.get("time_in_mins")) <= 0:
            issues.append(
                _rule_issue(
                    f"{prefix}_time_not_positive",
                    f"{row_path}.time_in_mins",
                    "Operation time must be greater than 0 when the operation is used in a BOM.",
                    operation=operation,
                )
            )


async def _validate_doc_payload_into(
    doctype: str,
    doc: dict[str, Any],
    *,
    client: ERPNextClient,
    settings: Settings,
    validation: dict[str, Any],
    schema_cache: dict[str, dict[str, Any]],
    exists_cache: dict[tuple[str, str], bool],
    path: str,
) -> None:
    schema = await _schema_for_validation(doctype, client=client, schema_cache=schema_cache)
    fields = schema["fields"]
    field_by_name = {field["fieldname"]: field for field in fields}

    for field in fields:
        fieldname = field["fieldname"]
        if field["reqd"] and not field["read_only"] and _is_empty(doc.get(fieldname)):
            validation["missing_required_fields"].append(
                {
                    "path": _join_path(path, fieldname),
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "label": field["label"],
                }
            )

    for fieldname in doc:
        if fieldname not in field_by_name and fieldname not in SYSTEM_FIELDS:
            validation["unknown_fields"].append(
                {"path": _join_path(path, fieldname), "doctype": doctype, "fieldname": fieldname}
            )

    for field in fields:
        fieldname = field["fieldname"]
        value = doc.get(fieldname)
        if _is_empty(value):
            continue

        fieldtype = field["fieldtype"]
        if fieldtype == "Link" and field.get("options"):
            await _validate_link_value(
                target_doctype=str(field["options"]),
                value=value,
                field=field,
                path=_join_path(path, fieldname),
                client=client,
                settings=settings,
                validation=validation,
                exists_cache=exists_cache,
            )
        elif fieldtype == "Dynamic Link":
            dynamic_doctype = doc.get(str(field["options"]))
            if _is_empty(dynamic_doctype):
                validation["warnings"].append(
                    {
                        "path": _join_path(path, fieldname),
                        "message": f"Dynamic Link target field is empty: {field['options']}",
                    }
                )
            else:
                await _validate_link_value(
                    target_doctype=str(dynamic_doctype),
                    value=value,
                    field=field,
                    path=_join_path(path, fieldname),
                    client=client,
                    settings=settings,
                    validation=validation,
                    exists_cache=exists_cache,
                )
        elif fieldtype == "Select":
            options = field.get("select_options") or []
            if options and value not in options:
                validation["invalid_select_values"].append(
                    {
                        "path": _join_path(path, fieldname),
                        "doctype": doctype,
                        "fieldname": fieldname,
                        "value": value,
                        "allowed": options,
                    }
                )
        elif fieldtype in {"Table", "Table MultiSelect"}:
            await _validate_child_table(
                child_doctype=str(field["options"]),
                value=value,
                path=_join_path(path, fieldname),
                client=client,
                settings=settings,
                validation=validation,
                schema_cache=schema_cache,
                exists_cache=exists_cache,
            )


async def _schema_for_validation(
    doctype: str, *, client: ERPNextClient, schema_cache: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if doctype not in schema_cache:
        schema_cache[doctype] = await _load_doctype_schema(doctype, client=client)
    return schema_cache[doctype]


async def _load_doctype_schema(doctype: str, *, client: ERPNextClient) -> dict[str, Any]:
    settings = getattr(client, "settings", None)
    if settings is not None:
        cached = _METADATA_CACHE.get(settings, doctype)
        if cached is not None:
            return cached
    doc = await client.get_doctype(doctype)
    custom_fields = await client.get_custom_fields(doctype)
    schema = _build_doctype_schema(doc, custom_fields)
    if settings is not None:
        _METADATA_CACHE.put(settings, doctype, schema)
    return schema


def _invalidate_metadata_after_write(doctype: str, client: ERPNextClient) -> None:
    settings = getattr(client, "settings", None)
    if settings is not None and doctype in {"Custom Field", "DocType", "Property Setter"}:
        _METADATA_CACHE.clear_target(settings)


async def _validate_link_value(
    *,
    target_doctype: str,
    value: Any,
    field: dict[str, Any],
    path: str,
    client: ERPNextClient,
    settings: Settings,
    validation: dict[str, Any],
    exists_cache: dict[tuple[str, str], bool],
) -> None:
    if not isinstance(value, str):
        validation["unknown_link_values"].append(
            {
                "path": path,
                "target_doctype": target_doctype,
                "value": value,
                "message": "Link value must be a string document name.",
            }
        )
        return

    cache_key = (target_doctype, value)
    if cache_key not in exists_cache:
        exists_cache[cache_key] = await client.doc_exists(target_doctype, value)
    if not exists_cache[cache_key]:
        validation["unknown_link_values"].append(
            {
                "path": path,
                "fieldname": field["fieldname"],
                "target_doctype": target_doctype,
                "value": value,
            }
        )


async def _validate_child_table(
    *,
    child_doctype: str,
    value: Any,
    path: str,
    client: ERPNextClient,
    settings: Settings,
    validation: dict[str, Any],
    schema_cache: dict[str, dict[str, Any]],
    exists_cache: dict[tuple[str, str], bool],
) -> None:
    if not isinstance(value, list):
        validation["invalid_child_tables"].append(
            {"path": path, "child_doctype": child_doctype, "message": "Expected a list."}
        )
        return

    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        if not isinstance(row, dict):
            validation["invalid_child_tables"].append(
                {
                    "path": row_path,
                    "child_doctype": child_doctype,
                    "message": "Expected each child row to be an object.",
                }
            )
            continue
        row_doctype = str(row.get("doctype") or child_doctype)
        await _validate_doc_payload_into(
            row_doctype,
            row,
            client=client,
            settings=settings,
            validation=validation,
            schema_cache=schema_cache,
            exists_cache=exists_cache,
            path=row_path,
        )


async def erpnext_health_check() -> dict[str, Any]:
    client = get_client()
    user = await client.get_logged_user()
    versions = await client.get_versions()
    settings = get_settings()
    return {
        "ok": True,
        "user": user,
        "versions": versions,
        **settings.public_status(),
    }


async def erpnext_doctype_schema(doctype: str) -> dict[str, Any]:
    return await _load_doctype_schema(doctype, client=get_client())


async def erpnext_bom_preflight(
    bom_name: str | None = None,
    doc: dict[str, Any] | None = None,
    include_doc: bool = False,
) -> dict[str, Any]:
    if bool(bom_name) == bool(doc):
        raise ValueError("Provide exactly one of bom_name or doc")

    client = get_client()
    proposed = await client.get_doc("BOM", str(bom_name)) if bom_name else dict(doc or {})
    proposed_doctype = str(proposed.get("doctype") or "BOM")
    if proposed_doctype != "BOM":
        raise ValueError("doc.doctype must be BOM")
    proposed["doctype"] = "BOM"

    validation = await _validate_doc_payload(
        "BOM",
        proposed,
        client=client,
        settings=get_settings(),
    )
    return _document_response(
        proposed,
        include_doc=include_doc,
        extra={
            "preflight": True,
            "dry_run": True,
            "source_bom": bom_name,
            "validation_scope": "schema_links_and_known_manufacturing_business_rules",
            **validation,
        },
    )


async def erpnext_link_search(
    doctype: str,
    text: str = "",
    filters: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit)
    client = get_client()
    primary = await client.search_link(doctype, text=text, filters=filters, limit=bounded_limit)
    rows = [_normalize_link_row(row, matched_on="frappe_search_link") for row in primary]

    if text:
        if await client.doc_exists(doctype, text):
            rows.insert(0, {"value": text, "matched_on": "name_exact"})

        name_matches = await client.list_docs(
            doctype,
            fields=["name"],
            filters=[["name", "like", f"%{text}%"]],
            limit=bounded_limit,
        )
        rows.extend(
            {"value": row["name"], "matched_on": "name_contains"}
            for row in name_matches
            if row.get("name")
        )
    elif not rows:
        name_matches = await client.list_docs(
            doctype,
            fields=["name"],
            limit=bounded_limit,
        )
        rows.extend(
            {"value": row["name"], "matched_on": "name_list"}
            for row in name_matches
            if row.get("name")
        )

    deduped = _dedupe_link_rows(rows)
    limited = deduped[:bounded_limit]
    return {
        "doctype": doctype,
        "text": text,
        "limit": bounded_limit,
        "count": len(limited),
        "source_count": len(primary),
        "deduped_count": len(deduped),
        "truncated": len(deduped) > len(limited),
        "complete": False,
        "scope": "Bounded search candidates; use doc_list for exhaustive pagination",
        "data": limited,
    }


async def erpnext_report_run(
    report_name: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await get_client().run_report(report_name, filters=filters)


async def erpnext_stock_balance(
    item_code: str,
    warehouse: str,
    company: str | None = None,
    include_child_warehouses: bool = False,
) -> dict[str, Any]:
    client = get_client()

    item_exists = await client.doc_exists("Item", item_code)
    warehouse_exists = await client.doc_exists("Warehouse", warehouse)
    company_exists = await client.doc_exists("Company", company) if company else None

    warehouse_company = None
    warehouse_matches_company = True
    if warehouse_exists:
        warehouse_doc = await client.get_doc("Warehouse", warehouse)
        warehouse_company = warehouse_doc.get("company")
        if company and warehouse_company:
            warehouse_matches_company = warehouse_company == company

    validation_error = None
    if not item_exists:
        validation_error = f"Item does not exist: {item_code}"
    elif not warehouse_exists:
        validation_error = f"Warehouse does not exist: {warehouse}"
    elif company and not company_exists:
        validation_error = f"Company does not exist: {company}"
    elif not warehouse_matches_company:
        validation_error = (
            f"Warehouse {warehouse} belongs to company {warehouse_company}, not {company}"
        )

    effective_company = company or warehouse_company
    validation = {
        "item_exists": item_exists,
        "warehouse_exists": warehouse_exists,
        "company_exists": company_exists,
        "warehouse_company": warehouse_company,
        "warehouse_matches_company": warehouse_matches_company,
        "effective_company": effective_company,
    }

    if validation_error:
        return {
            "ok": False,
            "validation_error": validation_error,
            "item_code": item_code,
            "warehouse": warehouse,
            "company": company,
            "include_child_warehouses": include_child_warehouses,
            **validation,
            "data": None,
        }

    exact_bins = await client.list_docs(
        "Bin",
        fields=["name", "item_code", "warehouse", "actual_qty", "projected_qty"],
        filters={"item_code": item_code, "warehouse": warehouse},
        limit=1,
    )
    data = await client.get_bin_details(
        item_code,
        warehouse,
        company=effective_company,
        include_child_warehouses=include_child_warehouses,
    )
    return {
        "ok": True,
        "item_code": item_code,
        "warehouse": warehouse,
        "company": company,
        "effective_company": effective_company,
        "include_child_warehouses": include_child_warehouses,
        "item_exists": item_exists,
        "warehouse_exists": warehouse_exists,
        "company_exists": company_exists,
        "warehouse_company": warehouse_company,
        "warehouse_matches_company": warehouse_matches_company,
        "bin_exists": bool(exact_bins),
        "exact_bin": exact_bins[0] if exact_bins else None,
        "company_total_stock_scope": (
            "All warehouses in effective_company; it is not restricted to the warehouse argument."
        ),
        "data": data,
    }


async def erpnext_process_trace(
    tag: str | None = None,
    company: str | None = None,
    item_code: str | None = None,
    sales_order: str | None = None,
    production_plan: str | None = None,
    work_order: str | None = None,
    purchase_order: str | None = None,
    subcontracting_order: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit, maximum=50)
    client = get_client()

    sales_order_doc = await _safe_trace_get(client, "Sales Order", sales_order)
    work_order_doc = await _safe_trace_get(client, "Work Order", work_order)
    production_item = item_code or (work_order_doc or {}).get("production_item")
    inferred_sales_order = sales_order or (work_order_doc or {}).get("sales_order")
    inferred_production_plan = production_plan or (work_order_doc or {}).get("production_plan")

    sales_order_filters = _trace_name_filters(tag, inferred_sales_order)
    if sales_order_filters:
        _append_filter(sales_order_filters, "company", "=", company)
    sales_item_filters: list[list[Any]] = []
    _append_filter(sales_item_filters, "parent", "=", inferred_sales_order)
    _append_filter(sales_item_filters, "item_code", "=", production_item)

    delivery_item_filters: list[list[Any]] = []
    _append_filter(delivery_item_filters, "against_sales_order", "=", inferred_sales_order)
    _append_filter(delivery_item_filters, "item_code", "=", production_item)

    purchase_order_filters = _trace_name_filters(tag, purchase_order)
    if purchase_order_filters:
        _append_filter(purchase_order_filters, "company", "=", company)
    purchase_item_filters: list[list[Any]] = []
    _append_filter(purchase_item_filters, "parent", "=", purchase_order)
    _append_filter(purchase_item_filters, "item_code", "=", production_item)
    receipt_item_filters: list[list[Any]] = []
    _append_filter(receipt_item_filters, "purchase_order", "=", purchase_order)
    _append_filter(receipt_item_filters, "item_code", "=", production_item)

    production_plan_filters = _trace_name_filters(tag, inferred_production_plan)
    if production_plan_filters:
        _append_filter(production_plan_filters, "company", "=", company)
    production_plan_item_filters: list[list[Any]] = []
    _append_filter(production_plan_item_filters, "parent", "=", inferred_production_plan)
    _append_filter(production_plan_item_filters, "item_code", "=", production_item)

    work_order_filters = _trace_name_filters(tag, work_order)
    _append_filter(work_order_filters, "production_item", "=", production_item)
    _append_filter(work_order_filters, "sales_order", "=", inferred_sales_order)
    _append_filter(work_order_filters, "production_plan", "=", inferred_production_plan)
    if work_order_filters:
        _append_filter(work_order_filters, "company", "=", company)
    work_order_operation_filters: list[list[Any]] = []
    _append_filter(work_order_operation_filters, "parent", "=", work_order)

    job_card_filters: list[list[Any]] = []
    _append_filter(job_card_filters, "work_order", "=", work_order)
    if not job_card_filters and tag:
        job_card_filters = [["name", "like", f"%{tag}%"]]

    stock_entry_filters: list[list[Any]] = []
    _append_filter(stock_entry_filters, "work_order", "=", work_order)
    if not stock_entry_filters and tag:
        stock_entry_filters = [["name", "like", f"%{tag}%"]]
    if stock_entry_filters:
        _append_filter(stock_entry_filters, "company", "=", company)
    stock_entry_detail_filters: list[list[Any]] = []
    _append_filter(stock_entry_detail_filters, "item_code", "=", production_item)

    subcontracting_order_filters = _trace_name_filters(tag, subcontracting_order)
    _append_filter(subcontracting_order_filters, "purchase_order", "=", purchase_order)
    if subcontracting_order_filters:
        _append_filter(subcontracting_order_filters, "company", "=", company)
    subcontracting_receipt_filters: list[list[Any]] = []
    _append_filter(
        subcontracting_receipt_filters, "subcontracting_order", "=", subcontracting_order
    )
    if not subcontracting_receipt_filters and tag:
        subcontracting_receipt_filters = [["name", "like", f"%{tag}%"]]
    if subcontracting_receipt_filters:
        _append_filter(subcontracting_receipt_filters, "company", "=", company)

    quality_filters: list[list[Any]] = []
    _append_filter(quality_filters, "item_code", "=", production_item)
    if work_order:
        _append_filter(quality_filters, "reference_name", "=", work_order)
    if not quality_filters and tag:
        quality_filters = [["name", "like", f"%{tag}%"]]

    sle_filters: list[list[Any]] = []
    _append_filter(sle_filters, "item_code", "=", production_item)
    gl_filters: list[list[Any]] = []
    if work_order:
        gl_filters.append(["voucher_no", "=", work_order])

    section_requests = {
        "sales_orders": _safe_trace_list(
            client,
            "Sales Order",
            fields=TRACE_DOCTYPE_FIELDS["Sales Order"],
            filters=sales_order_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "sales_order_items": _safe_trace_list(
            client,
            "Sales Order Item",
            fields=TRACE_DOCTYPE_FIELDS["Sales Order Item"],
            filters=sales_item_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "delivery_notes": _safe_trace_list(
            client,
            "Delivery Note",
            fields=TRACE_DOCTYPE_FIELDS["Delivery Note"],
            filters=[["name", "like", f"%{tag}%"]] if tag else None,
            limit=bounded_limit,
            require_filters=True,
        ),
        "delivery_note_items": _safe_trace_list(
            client,
            "Delivery Note Item",
            fields=TRACE_DOCTYPE_FIELDS["Delivery Note Item"],
            filters=delivery_item_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "purchase_orders": _safe_trace_list(
            client,
            "Purchase Order",
            fields=TRACE_DOCTYPE_FIELDS["Purchase Order"],
            filters=purchase_order_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "purchase_order_items": _safe_trace_list(
            client,
            "Purchase Order Item",
            fields=TRACE_DOCTYPE_FIELDS["Purchase Order Item"],
            filters=purchase_item_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "purchase_receipts": _safe_trace_list(
            client,
            "Purchase Receipt",
            fields=TRACE_DOCTYPE_FIELDS["Purchase Receipt"],
            filters=[["name", "like", f"%{tag}%"]] if tag else None,
            limit=bounded_limit,
            require_filters=True,
        ),
        "purchase_receipt_items": _safe_trace_list(
            client,
            "Purchase Receipt Item",
            fields=TRACE_DOCTYPE_FIELDS["Purchase Receipt Item"],
            filters=receipt_item_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "subcontracting_orders": _safe_trace_list(
            client,
            "Subcontracting Order",
            fields=TRACE_DOCTYPE_FIELDS["Subcontracting Order"],
            filters=subcontracting_order_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "subcontracting_receipts": _safe_trace_list(
            client,
            "Subcontracting Receipt",
            fields=TRACE_DOCTYPE_FIELDS["Subcontracting Receipt"],
            filters=subcontracting_receipt_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "production_plans": _safe_trace_list(
            client,
            "Production Plan",
            fields=TRACE_DOCTYPE_FIELDS["Production Plan"],
            filters=production_plan_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "production_plan_items": _safe_trace_list(
            client,
            "Production Plan Item",
            fields=TRACE_DOCTYPE_FIELDS["Production Plan Item"],
            filters=production_plan_item_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "work_orders": _safe_trace_list(
            client,
            "Work Order",
            fields=TRACE_DOCTYPE_FIELDS["Work Order"],
            filters=work_order_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "work_order_operations": _safe_trace_list(
            client,
            "Work Order Operation",
            fields=TRACE_DOCTYPE_FIELDS["Work Order Operation"],
            filters=work_order_operation_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "job_cards": _safe_trace_list(
            client,
            "Job Card",
            fields=TRACE_DOCTYPE_FIELDS["Job Card"],
            filters=job_card_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "stock_entries": _safe_trace_list(
            client,
            "Stock Entry",
            fields=TRACE_DOCTYPE_FIELDS["Stock Entry"],
            filters=stock_entry_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "stock_entry_details": _safe_trace_list(
            client,
            "Stock Entry Detail",
            fields=TRACE_DOCTYPE_FIELDS["Stock Entry Detail"],
            filters=stock_entry_detail_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "quality_inspections": _safe_trace_list(
            client,
            "Quality Inspection",
            fields=TRACE_DOCTYPE_FIELDS["Quality Inspection"],
            filters=quality_filters,
            limit=bounded_limit,
            require_filters=True,
        ),
        "stock_ledger_entries": _safe_trace_list(
            client,
            "Stock Ledger Entry",
            fields=TRACE_DOCTYPE_FIELDS["Stock Ledger Entry"],
            filters=sle_filters,
            order_by="posting_date desc, posting_time desc",
            limit=bounded_limit,
            require_filters=True,
        ),
        "gl_entries": _safe_trace_list(
            client,
            "GL Entry",
            fields=TRACE_DOCTYPE_FIELDS["GL Entry"],
            filters=gl_filters,
            order_by="posting_date desc",
            limit=bounded_limit,
            require_filters=True,
        ),
    }

    section_values = await asyncio.gather(*section_requests.values())
    sections = dict(zip(section_requests, section_values, strict=True))

    work_order_names = [
        row["name"] for row in sections["work_orders"]["data"] if isinstance(row.get("name"), str)
    ]
    traced_work_order_docs = await asyncio.gather(
        *(_safe_trace_get(client, "Work Order", name) for name in work_order_names[:5])
    )
    work_order_docs = [_doc_summary(doc) for doc in traced_work_order_docs if doc]

    return {
        "ok": True,
        "query": {
            "tag": tag,
            "company": company,
            "item_code": production_item,
            "sales_order": inferred_sales_order,
            "production_plan": inferred_production_plan,
            "work_order": work_order,
            "purchase_order": purchase_order,
            "subcontracting_order": subcontracting_order,
            "limit": bounded_limit,
        },
        "sales_order_summary": _doc_summary(sales_order_doc) if sales_order_doc else None,
        "work_order_summaries": work_order_docs,
        "complete": False,
        "scope": "Bounded diagnostic trace, not an exhaustive document inventory",
        "work_order_summary_count": len(work_order_docs),
        "work_order_summaries_truncated": len(work_order_names) > len(work_order_docs),
        "sections": sections,
    }


async def erpnext_party_balance(
    party_type: Literal["Customer", "Supplier"],
    party: str,
    company: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    if party_type not in {"Customer", "Supplier"}:
        raise ValueError("party_type must be Customer or Supplier")
    balance = await get_client().get_party_balance(
        party_type,
        party,
        company=company,
        date=date,
    )
    return {
        "party_type": party_type,
        "party": party,
        "company": company,
        "date": date,
        "balance": balance,
    }


erpnext_health_check.__doc__ = (
    "Check ERPNext connectivity, authenticated user, and installed app versions."
)

erpnext_doctype_schema.__doc__ = "Return ERPNext DocType metadata: fields, required fields, child tables, links, and docstatus support."

erpnext_bom_preflight.__doc__ = "Preflight an existing BOM or proposed BOM payload without writing. Resolves Routing operations and checks known ERPNext manufacturing rules such as positive operation time."

erpnext_link_search.__doc__ = "Search readable ERPNext Link field candidates, such as Customer, Item, Account, Warehouse, or Supplier."

erpnext_report_run.__doc__ = (
    "Run an allowed ERPNext report, such as General Ledger, Accounts Receivable, or Stock Balance."
)

erpnext_stock_balance.__doc__ = (
    "Return stock quantities for an item and warehouse using ERPNext bin details."
)

erpnext_process_trace.__doc__ = "Trace one ERPNext business flow across sales, buying, production planning, subcontracting, work orders, job cards, stock, quality inspections, delivery, and cost ledgers."

erpnext_party_balance.__doc__ = "Return ERPNext accounting balance for a Customer or Supplier."
