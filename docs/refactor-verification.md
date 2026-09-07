# 0.2.0 refactor verification

## Implemented

| Module | Responsibility |
| --- | --- |
| `config.py` | Default Safe Mode, pinned target/site/identity, explicit env selection, retired-config reporting |
| `runtime.py` | One immutable configuration and bounded HTTP connection pool per invocation |
| `requests.py` | HTTP request schema, source-count assertions and risk classification |
| `engine.py` | Preflight, immutable plans, batch approval, conflict checks, execution receipts and readback |
| `state.py` | Durable SQLite plans/artifacts, integrity checks, chunk reads and replay protection |
| `client.py` | Native HTTP transport, complete JSON/text/binary results, read helpers |
| `business.py` | Retained manufacturing validators, schema cache and specialized read tools |
| `server.py` | 31 MCP tools using the shared runtime and execution engine |

Version metadata comes from `pyproject.toml` (`0.2.0`). Runtime `__version__` reads installed
package metadata. The dependency lock includes FastMCP 3.4.2 and reproducible development tools.

## Evidence

- 41 automated tests cover request freezing, unknown RPC confirmation, mismatched approval
  hashes, target/identity isolation, expired/stale plans, concurrent/repeated execution,
  interrupted outcomes, batch partial failure, dependent create/submit, source hashes/counts,
  complete UTF-8/binary results, multipart/form/null/array bodies and native HTTP errors.
- In-process MCP tests exercise the real tool schema and confirmation flow. A subprocess stdio
  test starts the package entrypoint, enumerates 31 tools and prepares an unknown-method plan
  against an intentionally unreachable target without contacting it. A second subprocess test
  starts two profiles concurrently from a shared environment and verifies independent targets.
- Retained regressions cover manufacturing rules, custom fields, metadata caching, trace filters
  and structured server errors. Obsolete whitelist/draft-only deletion tests were replaced by
  the new safety contract.
- Ruff lint/format and `git diff --check` are part of local validation. CI is defined for Windows
  and Ubuntu in `.github/workflows/ci.yml`; see the repository Actions tab for hosted runs.
- Source distribution and wheel build successfully. `scripts/check_release.py` checks both for
  env secrets, `.repo`, business inputs, runtime databases and retired allowlist artifacts.

## Actual ERPNext target

A read-only probe against an already running local loopback site verified Frappe **16.32.0** and
ERPNext **16.33.0**, with HTTP **200** for both v1 and v2 document reads. An unknown RPC dry-run
produced a stored plan without invoking the method.

The local site had no pre-existing API identity available to the probe. It used the existing
local Administrator session for these read checks; no API key, user or configuration was
created. Token/bearer headers and identity binding were exercised in contract tests. Live
token authentication and live mutations were not verified by this probe.

No remote business site was called, no ERPNext business records were changed, and no Docker
services were started, rebuilt or restarted. Actual stock/accounting posting, external side
effects, every target-specific custom method and all version combinations remain outside the
verified scope. Preflight never claims to simulate those effects.

## Workspace and publication state

The original `0.1.6` implementation was preserved in `.repo/legacy-0.1.6`. Retired site-specific
configs and historical business reports were moved intact to `.repo/retired-0.1.6`, including
the user's pre-existing allowlist changes. Existing `.env`, `.env.test` and `.env.production`
were not edited. All archived/runtime material is excluded from Git and release packages.

The Windows startup failure was reproduced for both configured profiles: `uv run erpnext-mcp`
exited with error 32 while trying to replace an executable held by older sessions, before MCP
initialization. A fresh runtime environment and the `python -u -m erpnext_mcp` entrypoint avoid
startup-time package synchronization and console-script replacement. The client definitions
were backed up before updating only their launch commands. Concurrent handshakes using the
saved definitions returned 31 tools, version `0.2.0` and Safe Mode enabled for both profiles,
without any ERPNext network call. Existing sessions were not terminated.

The portable client example uses `uv run --no-sync python -u -m erpnext_mcp`; the Docker image
starts its installed Python directly. See [client setup](client-setup.md) for upgrade steps.

Repository publication starts from a clean source snapshot. Earlier Git history containing
site-specific reports remains in local archives rather than the published branch. No package
registry release or ERPNext deployment is implied by uploading the repository.
