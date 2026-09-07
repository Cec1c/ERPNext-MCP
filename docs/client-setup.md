# MCP client setup and startup recovery

## Install once, start without synchronization

From the project directory, run `uv sync --frozen` and create your `.env` from `.env.example`.
Set the target and credentials before starting the MCP. Python 3.12 is required.

The portable [mcp.json](../mcp.json) uses:

```text
uv --directory /absolute/path/to/erpnext-mcp run --no-sync python -u -m erpnext_mcp
```

`--no-sync` requires an already installed environment. Re-run `uv sync --frozen` deliberately
when upgrading dependencies. Do not put installation or upgrade commands in the client startup
path: multiple MCP profiles may start at the same time.

You can also start the environment's Python directly. On Windows it is
`.venv/Scripts/python.exe`; on Linux/macOS it is `.venv/bin/python`.

## Codex example

Use absolute paths and one explicit env file per instance. In your Codex configuration:

```toml
[mcp_servers.erpnext_test]
command = "C:/path/to/erpnext-mcp/.venv/Scripts/python.exe"
args = ["-u", "-m", "erpnext_mcp"]

[mcp_servers.erpnext_test.env]
ERPNEXT_ENV_FILE = "C:/path/to/erpnext-mcp/.env.test"

[mcp_servers.erpnext_prod]
command = "C:/path/to/erpnext-mcp/.venv/Scripts/python.exe"
args = ["-u", "-m", "erpnext_mcp"]

[mcp_servers.erpnext_prod.env]
ERPNEXT_ENV_FILE = "C:/path/to/erpnext-mcp/.env.production"
```

Create each env file from the example and edit it for that site. These names do not imply that
the test site is local. A missing explicitly selected env file fails instead of falling back
to a different site. Env-file values take precedence over stale process environment variables.

Start a new client session or reconnect both MCP entries. Call `erpnext_config_status` and
verify version `0.2.0`, `safe_mode=1` and each expected URL before business use. Initialization,
tool listing and config status do not require an ERPNext network call.

## Windows error 32 before initialization

`connection closed: initialize response` can mean that the process exited before speaking MCP.
Inspect stderr from the configured command. One verified cause during an upgrade is:

```text
failed to remove file .../Scripts/erpnext-mcp.exe
another process is using this file (os error 32)
```

Older `uv run erpnext-mcp` registrations attempt to synchronize the environment on every
startup. Existing Windows sessions lock that console-script executable, preventing its
replacement. Increasing a handshake timeout does not fix this failure.

Either disconnect the MCP instances using that environment, sync, and switch to the module
command above, or provision a new environment without interrupting existing sessions:

```powershell
$env:UV_PROJECT_ENVIRONMENT = '.repo/runtime-0.2.0'
uv sync --frozen --no-dev
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

Then point both client entries to the absolute
`.repo/runtime-0.2.0/Scripts/python.exe` path with `args = ["-u", "-m", "erpnext_mcp"]`.
Keep each entry's `ERPNEXT_ENV_FILE`. If a configuration manager such as CC Switch owns the
entries, update its saved definitions too so a later sync does not restore the old command.

The module entrypoint works from an installed wheel as well as a source checkout. Do not run
`src/erpnext_mcp/server.py` directly; its package-relative imports require module execution.
