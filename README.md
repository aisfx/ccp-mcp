# ccp-mcp

CCP（算力云平台）的 **MCP（Model Context Protocol）网关**——独立进程，让 Cursor / Claude 等支持 MCP 的 AI 客户端直接操作算力平台：查集群、提作业、看进度。

- **令牌库自有**（`app/store.py`），与 ccp-backend 解耦，独立演进
- **JSON-RPC 2.0** over streamable HTTP（`POST /mcp`），无状态设计，多副本天然可用
- **工具不直连 K8s**：每条 `tools/call` 经 HTTP 调 ccp-backend 的 `/api/v1/*`——权限、可见范围、计费拦截与控制台完全同一套，不另开权限模型

```
AI 客户端（Cursor / Claude / …）
      │  Bearer ccp-mcp-xxxx
      ▼
┌──────────────┐   POST /internal/sessions   ┌───────────────┐
│   ccp-mcp    │ ───────────────────────────▶│  ccp-backend  │
│  :8020       │ ◀── 每次调用铸 5min session ──│  :8010        │
└──────────────┘                             └───────────────┘
      │  /admin/tokens* （X-CCP-Internal-Key 互信）    │
      ▼                                              ▼
  令牌库（PG / JSON）                            K8s / Slurm
```

## 工作原理

1. **发令牌**：用户在控制台（或管理员直接调 `/admin/tokens`）创建 MCP 令牌。ccp-mcp 生成形如 `ccp-mcp-<hex>` 的明文，**只在返回那一刻可见**，库里只存 sha256。
2. **配客户端**：把明文令牌填进 AI 客户端的 MCP 配置（Bearer）。
3. **调工具**：客户端发 `tools/call` → ccp-mcp 拿令牌属主身份到 ccp-backend 铸一枚 5 分钟 session → 以该用户身份调平台 REST → 把结果转成「给 AI 看的文本」返回。

一次调用失败在哪儿都失败在 CCP 的业务规则上：没权限就是没权限，额度不足就是额度不足。

## 部署形态：PG（生产）> JSON 文件（开发）> 内存（测试）

| 模式 | 配置 | 适用场景 | 多副本 |
|---|---|---|---|
| **PG** | `CCP_MCP_DATABASE_URL` | 生产。与 ccp-backend 同库另一张表 `mcp_tokens` | ✅ 共享一张表，无丢更新 |
| **JSON 文件** | `CCP_MCP_DATA_PATH` | 开发 / 单副本。写时整体落盘 + mtime 感知 | ⚠️ 仅读可分载，写-写会丢更新，**必须单副本** |
| **内存** | 两者都不配 | 联调 / 单元测试，重启即丢 | ❌ |

> 历史教训：早期 JSON 落盘 + 多副本时，`mtime` 协议挡不住写-写竞争（读-改-写窗口内互相覆盖）。切 PG 后多副本丢更新问题根治，写频率低，全量 upsert 无压力。

## 配置

全部走环境变量，前缀 `CCP_MCP_`，支持 `.env` 文件（pydantic-settings）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CCP_MCP_BACKEND` | `http://127.0.0.1:8010` | ccp-backend 基址（集群内走 Service） |
| `CCP_MCP_INTERNAL_KEY` | 空 | 服务互信 key，与 ccp-backend 的 `CCP_INTERNAL_KEY` **同值**；空则拒绝所有 /admin 请求 |
| `CCP_MCP_DATABASE_URL` | 空 | PG 连接串，如 `postgresql://user:pass@host:5432/ccp`；配置则启用 PG 模式 |
| `CCP_MCP_DATA_PATH` | 空 | JSON 令牌库路径（如 `/data/tokens.json`），PG 优先于本项 |

`database_url` 兼容 SQLAlchemy 风格前缀：`postgresql+psycopg://` 会自动归一成 `postgresql://`。

## HTTP 接口

### MCP（AI 客户端 → 本服务）

`POST /mcp`，认证 `Authorization: Bearer ccp-mcp-xxxx`

无状态 JSON-RPC 2.0（不发 `Mcp-Session-Id`，协议版本 `2025-03-26`）：

| method | 行为 |
|---|---|
| `initialize` | 回协议版本、能力、服务端说明（含推荐调用顺序） |
| `ping` | 回空结果 |
| `tools/list` | 工具清单（JSON Schema 输入定义） |
| `tools/call` | 执行工具；业务错返回 `isError: true` + 给 AI 看的提示文本 |
| `notifications/*` | 无 id 的通知，回 `202 Accepted` |

认证失败回 `401` + `WWW-Authenticate: Bearer realm="ccp-mcp"`；方法不存在回 `-32601`；`GET /mcp` 回 405（无状态实现，仅支持 POST）。

### 令牌管理（ccp-backend 转发登录态 → 本服务）

需要请求头：

- `X-CCP-Internal-Key`：与配置的 internal key 一致，否则 401
- `X-CCP-User`：ccp-backend 转发来的当前用户名

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/admin/tokens` | 我的令牌列表；管理员可见全部 |
| `POST` | `/admin/tokens` | 发令牌，`{"name": "cursor"}`；**返回体里带明文 secret，唯一一次机会** |
| `POST` | `/admin/tokens/batch` | `{"action": "revoke"\|"purge", "ids": [...]}`，按 id 批量吊销 / 清除 |
| `DELETE` | `/admin/tokens/{id}` | active 令牌 → 吊销；revoked 令牌 → 彻底删除 |

每个用户最多 **8 枚** active 令牌（`TOKEN_LIMIT`），超出拒绝发放。

错误信封与 ccp 同形：

```json
{"success": false, "data": null, "errorCode": "unauthorized", "errorMessage": "...", "showType": 0}
```

### 健康探针

`GET /healthz` → `{"status": "ok"}`（`/mcp` 只认 POST，探针要单独打这个端点，否则 405 误判）。

## MCP 工具清单

| 工具 | 对应平台能力 | 典型场景 |
|---|---|---|
| `cluster_overview` | 集群资源总览：节点 / CPU / 内存 / GPU 三池总量与已分配 | 先看集群紧张不紧张 |
| `list_queues` | 队列（分区）清单：算力类型、GPU 型号、用量 | 提作业前挑队列 |
| `list_projects` | 我可提交的项目列表 | 提作业前拿项目短名 |
| `list_images` | 平台可用容器镜像及标签 | 提作业前挑镜像 |
| `submit_job` | 提交 Volcano 计算作业（支持多机多卡、env / ssh 插件） | 核心写操作 |
| `list_jobs` | 我的近期作业（按状态过滤） | 看提交结果 |
| `get_job` | 作业详情：状态、task 资源、Pod 列表、Pending 原因 | 排查为什么排队 |
| `delete_job` | 删作业（未完成的按 Terminated 归档；仅限自己的 / 管理员） | 清理 / 停止 |
| `list_gpu_instances` | 开发环境实例（GPU / 通用）：状态、规格、SSH 可达性 | 看自己在跑的实例 |

推荐的 AI 调用顺序（已写进 server instructions）：

```
list_queues → list_projects → submit_job → get_job
```

工具返回是**给 AI 看的文本**：列宽对齐、单位带上、数字带上下文，不是给前端的结构体。业务校验失败（如 `作业不存在：xxx`）以 `isError: true` 返回，AI 能据此自行纠错重试。

## 本地运行

```bash
# 依赖（Python 3.12+）
uv sync --all-groups
# 或
pip install -e ".[dev]"  # 需自行准备 pip 环境时用 uv.lock 对照版本

# 最小启动（内存令牌库，无内部互信——只能本机玩）
uvicorn app.main:create_app_factory --factory --port 8020

# 带配置启动
CCP_MCP_BACKEND=http://127.0.0.1:8010 \
CCP_MCP_INTERNAL_KEY=dev-key \
CCP_MCP_DATA_PATH=./tokens.json \
uvicorn app.main:create_app_factory --factory --reload --port 8020
```

## 测试

```bash
pytest          # 单测 + 接口测试
pytest -k token # 按名字过滤
```

覆盖：令牌全生命周期（发 / 列 / 吊销 / 清除 / 配额）、互信门、JSON-RPC 协议层（initialize / tools/list / tools/call / 未知方法 / 通知 / 鉴权）、工具调用对 ccp-backend 的 HTTP 交互（respx 模拟，含 401 重铸 session、业务错误信封透传）。

## 部署（k8s）

见 [`deploy/`](deploy/) 与 [`deploy/README.md`](deploy/README.md)：

- `deploy/ccp-mcp.yaml`：Deployment + Service + PVC（令牌 JSON 模式挂 `/data`）
- 镜像：`Dockerfile`，基础镜像 `python:3.12-slim`，暴露 `8020`
- Ingress：在 ccp-console 的 host 下**加一条 `/mcp` path** 指向本服务（优先级高于 `/`）

改 Ingress 用 `kubectl patch` 单独加 path，**不要 apply 整个 ingress.yaml**（线上还有别的注解）。

## 项目结构

```
ccp-mcp/
├── app/
│   ├── main.py        # FastAPI 应用：/mcp（JSON-RPC）、/admin/tokens*、/healthz
│   ├── settings.py    # 配置（pydantic-settings，前缀 CCP_MCP_）
│   ├── store.py       # 令牌库：PG / JSON 文件 / 内存 三态实现
│   └── tools.py       # MCP 工具定义 + 实现（HTTP 调 ccp-backend）
├── tests/
│   └── test_mcp_service.py
├── deploy/
│   ├── ccp-mcp.yaml   # k8s 清单（Deployment / Service / PVC）
│   └── README.md
├── Dockerfile
└── pyproject.toml
```

## 设计取舍速记

- **为什么令牌库不共用 ccp-backend 的 store**：那是单体内存库 + 请求前后 save/reload 的两副本同步协议，两个进程共写一个文件只会互相踩。令牌本来就为独立进程认证而生，自己一份最干净。
- **为什么无状态 JSON-RPC**：不发 `Mcp-Session-Id`，任何副本都能处理任何请求，水平扩容不需要粘性路由。
- **为什么每条 tools/call 铸新 session**：不缓存用户 session，令牌吊销即刻生效（下次调用重新认证时直接拒绝）；session 本身 5 分钟时效，开销可忽略。
- **明文令牌只出现一次**：库里只存 sha256 + 前 12 位前缀（认脸用），丢了只能吊销重建。

## 相关项目

- `ccp-backend` —— 平台单体后端（本服务依赖的 `/api/v1/*`、`/internal/*` 在此）
- `ccp-console` —— Web 控制台（令牌管理页转发 `/admin/tokens*` 到本服务）
