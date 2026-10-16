# ccp-mcp 部署（k1，ccp-system 命名空间）

- 令牌库：PVC（10Mi 就够）挂 `/data`，`CCP_MCP_DATA_PATH=/data/tokens.json`
- 互信：`CCP_MCP_INTERNAL_KEY` 与 ccp-backend 的 `CCP_INTERNAL_KEY` 同值（Secret `ccp-internal-key`）
- 后端基址：`http://ccp-backend.ccp-system.svc:8010`（集群内 Service）
- Ingress：`/mcp` 路由到本服务（ccp-console 那个 host 下加一条 path，优先级高于 `/`）

改动 Ingress 用 kubectl patch 单独加 path，**不要 apply 整个 ingress.yaml**（线上还有别的注解）。
