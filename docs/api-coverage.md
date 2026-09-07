# HTTP API coverage contract

The compatibility goal is representability of target-supported authenticated HTTP API behavior,
not a promise that all Frappe versions expose identical endpoints. The server does not deploy an
ERPNext app, bypass Frappe permissions, or install new server methods.

| API behavior | MCP route | Verification |
| --- | --- | --- |
| v1 resource CRUD, filters, ordering, pagination | Generic request / document tools | Contract tests; live read smoke |
| v2 document list/read/create/update/delete | Generic request with `/api/v2/document/...` | Contract tests; live read smoke |
| v2 metadata/count/copy/document methods | Generic request with target-native path | HTTP forwarding contract; exact target support remains server-defined |
| RPC GET/POST and custom whitelisted methods | `erpnext_call_method` / generic request | Unknown-method prepare/confirm/execute tests |
| Submit/cancel/workflow/mapping | Convenience or native RPC/document method | Frozen-payload and dependent-batch tests; real posting not simulated |
| Report/export/printing | Report convenience / generic request | JSON, text and binary preservation tests |
| Upload File / multipart | Finalized artifact + request `files` | Multipart fixture test |
| Download public/private file | Generic same-origin GET | Complete binary artifact test |
| URL-encoded form / raw request body | `form` / `body_base64` / artifact | HTTP body parity tests |
| Null JSON, repeated query parameters, HTTP errors | Generic request | Transport contract tests |
| Token / OAuth bearer identity | Environment configuration | Header and identity-isolation tests |

Runtime route errors such as 403/404/405 are returned with their original HTTP status and complete
body. A missing v2 route is not silently redirected to a v1 endpoint with different semantics.
Cookies are not exposed in response headers and password/session login is not a persistent
authentication mode; configure token or bearer identity instead.

## Boundaries

- Client preflight checks request integrity, readable schema/link metadata and retained known
  manufacturing rules. Missing permissions/schema are disclosed and become sensitive review.
- Server-side `validate`/submit/cancel hooks, accounting/stock effects and external side effects
  are verified only by the server's actual execution and appropriate domain readbacks.
- Multi-request transactions are not atomic. Generic precondition reads have a race window.
- Storage is complete within configured limits. The MCP cannot infer rows omitted by the source
  or by an upstream API before it received the response.
- Safe Mode confirmation is agent-attested in 0.2. Trusted host approval is an extension point.
