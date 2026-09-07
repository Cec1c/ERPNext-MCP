# Usage and operations

An ERPNext/Frappe API bridge with **Safe Mode**: prepare complete requests, confirm sensitive
operations once, execute the frozen plan, and retain complete results.

The server supports generic REST and RPC calls alongside manufacturing, stock, accounting and
document conveniences. It does not maintain a DocType or method admission whitelist. ERPNext's
HTTP API permissions, whitelisted-method rules and business validations remain authoritative.

## Quick start

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Edit ERPNEXT_URL and credentials in .env; safe_mode=1 is the default.
uv run --no-sync python -u -m erpnext_mcp
```

Install dependencies before starting the MCP. See [client setup and startup recovery](client-setup.md).

Use the portable [`mcp.json`](../mcp.json) example after replacing its two absolute paths. Set
`ERPNEXT_ENV_FILE` explicitly for each MCP instance. The file is re-read at the start of each
invocation, and its values override stale process environment variables. A missing explicitly
selected file is an error, never a reason to fall back to another site. One invocation pins its
target and identity until it finishes.

Token authentication and OAuth bearer tokens are supported. `ERPNEXT_ACCESS_TOKEN`, when set,
takes precedence over `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET`.
For multi-site deployments, `ERPNEXT_SITE` pins the `X-Frappe-Site-Name` header and is included
in plan identity. Individual calls cannot override that header or the configured Host.

## Safe Mode

```dotenv
safe_mode=1
```

| Operation | `safe_mode=1` (default) | `safe_mode=0` |
| --- | --- | --- |
| Known read | Execute | Execute |
| Ordinary document write | Automatic preflight, then execute | Execute directly |
| Sensitive operation | Prepare, obtain one user approval, execute plan | Execute directly |
| Unknown RPC/route | Sensitive, confirmable; no whitelist edit | Execute directly |
| Input checks and complete output storage | Enabled | Enabled |

Sensitive operations include delete, submit/cancel, system/permission/ledger changes, submitted
document changes, child-table replacement, mutating batches and methods whose side effects are
unknown. The risk catalogue describes known behavior; it is not an admission list. A GET request
to an unknown RPC is still sensitive. Dispatch overrides such as `cmd` also require review.

`dry_run=true` prepares and stores a plan without sending its requested mutation. It can perform
read-only metadata/document checks. It is **not** an ERPNext save/submit simulation: hooks, stock
and accounting posting, external effects and custom controller logic are not proven. Schema
findings are review material; incomplete metadata escalates to sensitive review instead of
silently dropping fields or denying a new DocType.

### One approval per concrete plan

1. Call a write tool with `dry_run=true`, or call `erpnext_batch_prepare`.
2. Inspect the returned `plan_id`, `request_sha256`, target, scope and preflight findings.
   `erpnext_plan_inspect` provides an artifact containing the full frozen request.
3. For a sensitive plan, the agent asks the user once for that exact task/batch. An existing
   explicit approval of the same scope can be reused; a new target or expanded scope cannot.
4. Call `erpnext_plan_execute(plan_id, request_sha256, user_confirmation)` using the actual
   approval text. Execution uses stored payloads; do not reconstruct the request.
5. Read the receipt and any available readback. A successful HTTP request does not prove every
   accounting, stock, email or background-job effect.

Confirmation in this release is **agent attestation**. The MCP records the supplied user reply
and binds it to the plan, but cannot independently authenticate a human conversation. It does
not claim that a model-supplied string is an unforgeable approval token. Client-native approval
and elicitation are future adapters, not prerequisites for the current agent workflow.

Plans expire after one hour by default and are bound to the target and configured credentials.
Their payload hashes must match at execution. Recorded running/completed/failed/unknown plans
cannot be replayed, including after process restart. These guarantees apply to one plan ID;
creating a new plan is a new operation and cannot provide universal business idempotency.

## Tools

| Family | Tools |
| --- | --- |
| Generic API | `erpnext_api_request`, `erpnext_call_method` |
| Plans and batches | `erpnext_batch_prepare`, `erpnext_batch_prepare_from_artifact`, `erpnext_plan_list`, `erpnext_plan_inspect`, `erpnext_plan_execute` |
| Complete input/output | `erpnext_input_begin`, `erpnext_input_append`, `erpnext_input_finalize`, `erpnext_result_read` |
| Connection and schema | `erpnext_config_status`, `erpnext_health_check`, `erpnext_doctype_schema` |
| Documents | `erpnext_doc_list`, `erpnext_doc_get`, `erpnext_doc_create`, `erpnext_doc_update`, `erpnext_doc_delete`, `erpnext_doc_submit`, `erpnext_doc_cancel`, `erpnext_doc_map` |
| Business conveniences | `erpnext_bom_preflight`, `erpnext_bom_create_revision`, `erpnext_work_order_make_stock_entry`, `erpnext_naming_series_configure`, `erpnext_link_search`, `erpnext_report_run`, `erpnext_stock_balance`, `erpnext_party_balance`, `erpnext_process_trace` |

The generic request tool accepts API v1/v2 paths and other same-origin HTTP endpoints. Route
availability is determined by the target version. It preserves HTTP status, JSON/text/binary
bodies and response headers (excluding session cookies). It supports JSON including explicit
`null`, repeated query parameters, form bodies, multipart uploads and raw base64 bodies.
Authentication/Host/framing headers are managed by the connection. Redirects are returned for
inspection, never followed automatically with the configured credentials.

### Custom RPC

```json
{
  "method": "my_app.api.perform_operation",
  "args": {"document": "DOC-001"},
  "dry_run": true
}
```

Unknown methods work through the same prepare/confirm/execute flow without code or whitelist
changes. Server-side `@frappe.whitelist` and permissions still apply. This is an HTTP API bridge,
not an arbitrary Python, SQL or shell execution service.

### Native v2 request

```json
{
  "request": {
    "method": "PATCH",
    "path": "/api/v2/document/Task/TASK-001",
    "json_body": {"subject": "Reviewed task"},
    "expected_modified": "2026-09-07 09:00:00.000000"
  },
  "dry_run": true
}
```

### Batch with a dependent submit

Prepare an ordered array of `RequestSpec` objects with `expected_count` equal to the source
operation count. Later JSON body values can reference earlier response JSON using `bindings`:

```json
{
  "method": "POST",
  "path": "/api/method/frappe.client.submit",
  "json_body": {"doc": null},
  "bindings": {"/doc": {"step": 0, "pointer": "/data"}}
}
```

The binding expression is frozen and the resolved request hash is recorded. A batch stops on
the first upstream failure. It is **not atomic**: prior successful requests are not rolled back.
Version preconditions are checked before the batch starts; generic REST delete/cancel checks
are best effort, not a server-side atomic compare-and-swap. The document-update convenience
also sends the frozen `modified` value through native save validation.

## Input and output completeness

For large input, compute the source byte count and SHA-256 independently. Begin an upload,
append ordered base64 chunks (at most 256 KiB each), then finalize. A partial, extra-length or
hash-mismatched upload cannot be used. Reference a finalized artifact with `json_artifact_id`,
`body_artifact_id`, or multipart `files[].artifact_id`. Use `expected_lengths`, such as
`{"/items": 120}`, to verify child-table/array counts. For large batches, upload the entire JSON
request array and use `erpnext_batch_prepare_from_artifact`.

A hash proves that received bytes stay unchanged. It cannot detect content that the agent
omitted before its first submission; source files/counts and user review provide that boundary.

Responses exceeding the inline budget are saved in full and identified by `result_id` or
`body.artifact_id`. `erpnext_result_read` returns base64 byte chunks, total bytes, SHA-256,
`has_more` and `next_offset`. Its optional RFC 6901 JSON pointer selects a subtree. Join bytes
before decoding UTF-8, since a chunk can end inside a multibyte character.

`transport_complete` means the HTTP response body was received in full, not that the server
returned every record. `erpnext_doc_list` exposes pagination and does not silently clamp page
size. `has_more=null` means a full page without a proven total. Search candidates and business
traces explicitly remain bounded. Concurrent changes can affect live pagination.

If the configured artifact size is exceeded, the response is incomplete and a write receipt is
marked unknown rather than retried. Increase the deployment limit or use native pagination.

## State, deployment and recovery

Local `.mcp-state/state.sqlite3` stores frozen requests, complete responses and approval
attestations. It contains business data. Keep it private, persist it across process restarts,
and exclude it from Git, images and release archives. Plan expiry does not delete receipts or
artifacts. Storage limits stop new storage; there is no automatic deletion of audit evidence.
Archive old state while the MCP is stopped when needed.

The supported deployment model is one trusted operator per configured MCP identity. The
default transport is stdio. Streamable HTTP can be enabled with `MCP_TRANSPORT=http`; keep the
default loopback binding or provide authentication/isolation in the hosting layer. This server
does not provide multi-tenant caller authentication.

```powershell
docker build -t erpnext-mcp .
docker run --rm -i --env-file .env -e ERPNEXT_STATE_DIR=/state -v erpnext-mcp-state:/state erpnext-mcp
```

A timeout/disconnection is reported as an unknown outcome. Use `erpnext_plan_list` to recover lost plan IDs, then inspect receipts and ERPNext
before issuing a replacement plan. Do not assume a failed HTTP response proves that every
database or external side effect rolled back.

## Development and migration

```powershell
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
uv run erpnext-mcp-smoke --expect-url http://127.0.0.1:8080
```

The smoke command is read-only and refuses a target mismatch. Unit/contract tests use HTTP
fixtures and in-process MCP clients; they are not evidence of production posting behavior.
See [API coverage](api-coverage.md), [migration](migration-0.2.md) and
[refactor verification](refactor-verification.md).

Version 0.2 changes write responses to plan/execution envelopes and replaces exact-name delete
confirmation with plan-scoped approval. Old allowlist settings are ignored and reported by
`erpnext_config_status`. Existing env files are not rewritten. Restart/reconnect the MCP after
upgrading source; a long-running process does not reload Python modules automatically.

Official references: [REST API](https://docs.frappe.io/framework/user/en/api/rest),
[API v2 source](https://github.com/frappe/frappe/blob/version-16/frappe/api/v2.py),
[transaction model](https://docs.frappe.io/framework/user/en/api/database).
