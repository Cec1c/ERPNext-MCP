<div align="center">

# ERPNext MCP

[![Python 3.12](https://img.shields.io/static/v1?label=Python&message=3.12&color=3776AB&style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/static/v1?label=Framework&message=FastMCP&color=2563EB&style=flat-square)](https://gofastmcp.com/)
[![Frappe API v1 and v2](https://img.shields.io/static/v1?label=Frappe%20API&message=v1%20%2F%20v2&color=0089FF&style=flat-square)](docs/api-coverage.md)
[![Safe Mode enabled by default](https://img.shields.io/static/v1?label=Safe%20Mode&message=default%20on&color=16A34A&style=flat-square)](#safe-mode)

An ERPNext / Frappe MCP server for AI agents, with native HTTP API access, sensitive-operation approval, and complete request and response storage.

[简体中文](README.zh-CN.md) ｜ [Quick start](#quick-start) ｜ [Client setup](docs/client-setup.md) ｜ [Usage guide](docs/usage.md)

</div>

## What it does

- **Use REST and RPC without maintaining an MCP whitelist.** Call API v1/v2, custom methods, document actions and file endpoints. ERPNext permissions and server-side whitelisted-method rules still apply.
- **Review sensitive work with Safe Mode.** Prepare an immutable request or batch, obtain one approval for its exact scope, then execute the stored plan.
- **Keep large payloads intact.** Verify input byte counts, SHA-256 hashes and array lengths; retrieve complete responses through chunked artifacts.
- **Keep useful ERP tools close.** 31 tools cover documents, schemas, manufacturing preflight, stock, balances, reports and process tracing.

## Quick start

Requires **Python 3.12** and [uv](https://docs.astral.sh/uv/). Run these commands from a local checkout:

```powershell
git clone https://github.com/Cec1c/ERPNext-MCP.git
cd ERPNext-MCP
uv sync --frozen
Copy-Item .env.example .env
```

On Linux/macOS, use `cp .env.example .env`. Edit the new file with your ERPNext URL and credentials:

```dotenv
ERPNEXT_URL=https://your-erpnext.example.com
ERPNEXT_API_KEY=your-api-key
ERPNEXT_API_SECRET=your-api-secret
safe_mode=1
```

Configure your MCP client using [mcp.json](mcp.json), replacing its absolute paths. Set a separate `ERPNEXT_ENV_FILE` for each site. Dependencies are installed above; startup intentionally skips synchronization:

```powershell
uv run --no-sync python -u -m erpnext_mcp
```

This starts a stdio server that waits for the MCP client. Once connected, `erpnext_config_status` should report version `0.2.0`, `safe_mode=1` and the intended target. OAuth bearer tokens are also supported through `ERPNEXT_ACCESS_TOKEN`.

For Codex configuration, multiple instances and Windows startup recovery, see [client setup](docs/client-setup.md).

## Safe Mode

Safe Mode is the default operating model, configured in `.env` with `safe_mode=1`.

| Operation | `safe_mode=1` | `safe_mode=0` |
| --- | --- | --- |
| Known read | Execute | Execute |
| Ordinary document write | Automatic preflight, then execute | Execute directly |
| Sensitive operation or unknown RPC | Prepare → one approval → execute plan | Execute directly |
| Input integrity and complete output storage | Enabled | Enabled |

Delete, submit/cancel, permission and ledger changes, child-table replacement, mutating batches and unknown side effects are sensitive. A new custom method uses the same confirmation flow without an MCP whitelist edit.

1. Prepare with `dry_run=true` or `erpnext_batch_prepare`.
2. Review the target, scope, preflight findings, `plan_id` and `request_sha256`.
3. The agent asks the user once for that exact operation or batch; existing explicit approval of the same scope can be reused.
4. Execute through `erpnext_plan_execute` with the plan ID, hash and actual approval text.

**Dry-run is preflight, not a server-side transaction simulation.** It does not prove that hooks, stock/accounting posting or external effects will succeed. Confirmation records the agent's attestation of user approval; it cannot independently authenticate a human reply. Batches execute in order and are **not atomic**.

## Complete requests and results

Large inputs use ordered uploads with independently supplied byte counts and SHA-256 hashes. Frozen plans prevent execution from reconstructing a truncated request. Large responses are stored in full and read through `erpnext_result_read`; paginated APIs still require pagination.

Hashes cannot detect content omitted before the first upload. Preserve source counts and review the prepared scope. Timeouts and incomplete write responses can mean an **unknown outcome**: inspect the receipt and ERPNext before retrying.

Local `.mcp-state/` contains requests, responses and approval records. Keep it private and persistent. The default deployment is stdio for one trusted operator per identity; HTTP hosting needs authentication and isolation in the hosting layer.

## Documentation

| Task | Guide |
| --- | --- |
| Configure clients or repair startup | [Client setup](docs/client-setup.md) |
| Use tools, files, batches and result artifacts | [Usage and operations](docs/usage.md) |
| Understand supported HTTP behavior and limits | [API coverage](docs/api-coverage.md) |
| Upgrade from 0.1 | [Migration guide](docs/migration-0.2.md) |
| Review tests and live verification boundaries | [Verification evidence](docs/refactor-verification.md) |

## Development

```powershell
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv build
uv run --no-sync python scripts/check_release.py
```

CI runs on Windows and Ubuntu. Tests cover MCP stdio handshakes and API/safety contracts; the live read-only probe covered Frappe 16 / ERPNext 16. They do not establish production write behavior or compatibility with every server version.

Built with [FastMCP](https://gofastmcp.com/) for [Frappe's HTTP API](https://docs.frappe.io/framework/user/en/api/rest).
