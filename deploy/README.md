# ccp-mcp 部署

参考 [`ccp-mcp.yaml`](ccp-mcp.yaml)（Deployment + Service + PVC），按实际环境调整以下几项：

- **令牌库**
  - JSON 模式（默认清单）：PVC 挂 `/data`，设置 `CCP_MCP_DATA_PATH=/data/tokens.json`，此时 `replicas` 必须为 1（写-写竞争会丢更新）
  - PG 模式（推荐生产）：设置 `CCP_MCP_DATABASE_URL=postgresql://user:pass@host:5432/ccp`，可去掉 PVC，副本数按流量扩展
- **服务互信**：`CCP_MCP_INTERNAL_KEY` 与 ccp-backend 的 `CCP_INTERNAL_KEY` 同值，建议通过 Secret 注入（清单里引用的是 `ccp-internal-key`，名字可自定义）
- **后端基址**：`CCP_MCP_BACKEND`，集群内一般为 `http://ccp-backend.<namespace>.svc:8010`
- **对外暴露**：在 Ingress / 网关上把 `/mcp` 路径路由到本服务；探针走 `/healthz`
