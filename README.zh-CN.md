<div align="center">

# ERPNext MCP

[![Python 3.12](https://img.shields.io/static/v1?label=Python&message=3.12&color=3776AB&style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![框架 FastMCP](https://img.shields.io/static/v1?label=%E6%A1%86%E6%9E%B6&message=FastMCP&color=2563EB&style=flat-square)](https://gofastmcp.com/)
[![Frappe API v1 和 v2](https://img.shields.io/static/v1?label=Frappe%20API&message=v1%20%2F%20v2&color=0089FF&style=flat-square)](docs/api-coverage.md)
[![Safe Mode 默认开启](https://img.shields.io/static/v1?label=Safe%20Mode&message=%E9%BB%98%E8%AE%A4%E5%BC%80%E5%90%AF&color=16A34A&style=flat-square)](#safe-mode)

面向 AI agent 的 ERPNext / Frappe MCP 服务，提供原生 HTTP API 调用、敏感操作确认，以及完整的请求与结果留存。

[English](README.md) ｜ [快速开始](#快速开始) ｜ [客户端配置](docs/client-setup.md) ｜ [使用文档](docs/usage.md)

</div>

## 能做什么

- **调用 REST 和 RPC，无需维护 MCP 白名单。** 支持 API v1/v2、自定义方法、单据操作与文件接口；ERPNext 自身的权限和服务端方法白名单仍然生效。
- **通过 Safe Mode 审阅敏感操作。** 先保存不可变的请求或批次，用户对明确范围确认一次，再执行已保存的计划。
- **保持大输入与大结果完整。** 校验输入字节数、SHA-256 和数组条数，通过分块读取取回完整响应。
- **保留常用 ERP 工具。** 31 个工具覆盖单据、元数据、制造预检、库存、余额、报表和业务追踪。

## 快速开始

需要 **Python 3.12** 和 [uv](https://docs.astral.sh/uv/)。在本地项目目录执行：

```powershell
git clone https://github.com/Cec1c/ERPNext-MCP.git
cd ERPNext-MCP
uv sync --frozen
Copy-Item .env.example .env
```

Linux/macOS 使用 `cp .env.example .env`。在新文件中填写 ERPNext 地址和凭据：

```dotenv
ERPNEXT_URL=https://your-erpnext.example.com
ERPNEXT_API_KEY=your-api-key
ERPNEXT_API_SECRET=your-api-secret
safe_mode=1
```

参照 [mcp.json](mcp.json) 配置 MCP 客户端，替换其中的绝对路径；多站点实例各自指定 `ERPNEXT_ENV_FILE`。依赖在上一步安装，日常启动跳过同步：

```powershell
uv run --no-sync python -u -m erpnext_mcp
```

该命令启动 stdio 服务，等待 MCP 客户端连接。连接后调用 `erpnext_config_status`，应返回版本 `0.2.0`、`safe_mode=1` 和预期站点。也支持通过 `ERPNEXT_ACCESS_TOKEN` 使用 OAuth bearer token。

Codex 配置、多实例与 Windows 启动故障处理见[客户端配置](docs/client-setup.md)。

## Safe Mode

Safe Mode 是项目默认的工作方式，在 `.env` 中配置 `safe_mode=1`。

| 操作 | `safe_mode=1` | `safe_mode=0` |
| --- | --- | --- |
| 已知只读操作 | 直接执行 | 直接执行 |
| 普通单据写入 | 自动预检后执行 | 直接执行 |
| 敏感操作或未知 RPC | 准备计划 → 确认一次 → 执行 | 直接执行 |
| 输入完整性校验与完整结果留存 | 开启 | 开启 |

删除、提交/取消、权限与账务变更、子表替换、写入批次，以及副作用未知的调用均视为敏感操作。新增自定义方法沿用同一确认流程，无需修改 MCP 白名单。

1. 使用 `dry_run=true` 或 `erpnext_batch_prepare` 准备计划。
2. 审阅目标、范围、预检结果、`plan_id` 和 `request_sha256`。
3. Agent 向用户确认这一次操作或批次；同一范围已有明确授权时可复用。
4. 将计划 ID、哈希和用户的实际确认文本传给 `erpnext_plan_execute`。

**Dry-run 是预检，不是服务端事务模拟。** 它不能保证 hooks、库存/账务过账或外部副作用必定成功。确认记录由 agent 转述用户授权，MCP 无法独立验证这段文本确实来自用户。批次按顺序执行，**不具备整体原子性**。

## 防截断与结果留存

大输入按顺序上传，并使用独立计算的字节数与 SHA-256 校验。执行阶段使用已冻结计划，避免重新拼装时丢失内容。大响应完整保存，通过 `erpnext_result_read` 分块取回；分页接口仍需继续翻页。

哈希无法发现首次上传前就被遗漏的内容，因此仍需保留源数据条数并审阅计划范围。写入超时或响应不完整可能意味着**结果未知**：重试前先检查执行记录和 ERPNext 实际状态。

本地 `.mcp-state/` 保存请求、响应和确认记录，应私密保存并持久化。默认通过 stdio 为每个身份的单一可信操作者提供服务；以 HTTP 托管时，需在托管层提供身份验证和隔离。

## 文档

| 任务 | 文档 |
| --- | --- |
| 配置客户端、修复启动失败 | [客户端配置](docs/client-setup.md) |
| 使用工具、文件、批次与完整结果 | [使用与运维](docs/usage.md) |
| 了解 HTTP 能力与边界 | [API 覆盖说明](docs/api-coverage.md) |
| 从 0.1 升级 | [迁移指南](docs/migration-0.2.md) |
| 查看测试与实测范围 | [验证记录](docs/refactor-verification.md) |

## 开发

```powershell
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv build
uv run --no-sync python scripts/check_release.py
```

CI 在 Windows 和 Ubuntu 上运行。测试覆盖 MCP stdio 握手、API 与安全契约；本地只读实测覆盖 Frappe 16 / ERPNext 16，不代表已验证生产写入或全部服务端版本。

基于 [FastMCP](https://gofastmcp.com/) 构建，对接 [Frappe HTTP API](https://docs.frappe.io/framework/user/en/api/rest)。
