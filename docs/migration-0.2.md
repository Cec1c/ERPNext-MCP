# Migration from 0.1 to 0.2

0.2 replaces curated admission lists with Safe Mode and introduces a new write-response
contract. It is an intentional pre-1.0 breaking release.

1. Preserve existing env files and credentials. Set `safe_mode=1` explicitly, or rely on its
   default. Only `0` and `1` are accepted; per-call arguments cannot disable it.
2. Remove retired `ERPNEXT_ALLOWED_*`, `ERPNEXT_READ_ALLOWED_*`,
   `ERPNEXT_DELETE_ALLOWED_*` and `ERPNEXT_ALLOWLIST_FILE` settings when convenient. They have
   no effect in 0.2. Config status lists ignored keys. TOML whitelist files are never loaded.
3. Persist `ERPNEXT_STATE_DIR` outside the application image. Do not share it across untrusted
   operators. Identity changes make old artifacts/plans inaccessible through that identity.
4. Adapt write callers: expect `prepared`/`confirmation_required` or an execution receipt.
   Inspect the plan, obtain one in-scope user approval and execute its ID/hash. Old
   `confirm_name` and `include_doc` write arguments were removed. Full responses are retrievable
   from artifacts instead of being suppressed or copied into every tool reply.
5. Remove any expectation that delete is draft-only in MCP. ERPNext now decides which states
   can be deleted. Safe Mode still classifies every delete as sensitive.
6. A doc-list full page without a total has `has_more=null`; follow `next_offset` until exhaustion.
   Search/trace tools reject excessive requested limits instead of silently lowering them.
7. `transaction_rolled_back=null` means unknown. Do not retry a write from this flag. A stopped
   batch may contain committed earlier requests.
8. Install dependencies separately, then start with `uv run --no-sync python -u -m erpnext_mcp`
   or the installed environment's Python. See [client setup](client-setup.md) for Windows
   launcher locks and parallel test/production profiles. Reconnect/restart MCP after upgrading
   Python source. Check config status before business use.

Client-side tool approval policies are independent of Safe Mode. This release does not modify
host/client trust settings or suppress prompts imposed by the host.

Legacy code, site-specific configs (including existing local changes) and historical business
reports were archived locally under `.repo/`. They are not runtime dependencies and are excluded
from Git/Docker/package output. The active application lives only under `src/erpnext_mcp/`.

Connection pooling now lasts for one pinned invocation. This prevents a target switch or another
invocation's eviction from closing an in-flight request. Within an invocation, concurrent trace
reads share the configured bounded HTTP connection pool. Metadata caching remains target and
credential scoped; mutations invalidate cached metadata.
