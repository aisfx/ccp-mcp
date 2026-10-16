"""
ccp-mcp：CCP 平台的 MCP（Model Context Protocol）网关，独立进程。

- **令牌**：`/admin/tokens*`（X-CCP-Internal-Key + X-CCP-User 认证，ccp-backend 转发来的
  登录态请求）——库在本进程（app/store.py，JSON 落盘），明文只存 sha256。
- **JSON-RPC**：`POST /mcp`（Bearer = MCP 令牌）——streamable HTTP 上的 JSON-RPC 2.0，
  无状态（不发 Mcp-Session-Id），多副本天然可用。
- **工具**：每条 tools/call 先拿令牌属主在 ccp-backend 铸一枚 5 分钟 session
  （POST /internal/sessions），再以用户身份调 `/api/v1/*`——权限、可见范围、计费拦截
  与控制台同一套，不另开权限模型。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.settings import Settings, get_settings
from app.store import TokenStore, view

PROTOCOL_VERSION = "2025-03-26"


def create_app(
    settings: Settings | None = None, store: TokenStore | None = None
) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(title="ccp-mcp")
    app.state.settings = cfg
    app.state.store = store or TokenStore(cfg.data_path)
    _register_admin(app)
    _register_rpc(app)
    _register_tools(app)
    return app


def _fail(code: str, message: str, status: int = 400, show_type: int = 0) -> JSONResponse:
    """与 ccp 同形的业务错误信封（success=false）。"""
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "errorCode": code, "errorMessage": message, "showType": show_type},
    )


# ------------------------------------------------------------------ 令牌管理（ccp-backend 转发来）


def _register_admin(app: FastAPI) -> None:
    @app.middleware("http")
    async def _gate(request: Request, call_next):
        """admin 路由的互信门：X-CCP-Internal-Key 对了才放行。"""
        if request.url.path.startswith("/admin"):
            expected = app.state.settings.internal_key
            got = request.headers.get("X-CCP-Internal-Key", "")
            if not expected or got != expected:
                return _fail("unauthorized", "内部 key 不对", 401)
        return await call_next(request)

    @app.get("/admin/tokens")
    async def list_tokens(request: Request, user: str = Header(alias="X-CCP-User")) -> JSONResponse:
        admin = await _is_admin(app, user)
        items = app.state.store.list_for(user, admin)
        return JSONResponse({"success": True, "data": {"list": [view(t) for t in items], "total": len(items)}, "errorCode": "", "errorMessage": ""})

    @app.post("/admin/tokens")
    async def create_token(request: Request, user: str = Header(alias="X-CCP-User")) -> JSONResponse:
        body = await request.json()
        try:
            record, secret = app.state.store.issue(str(body.get("name") or ""), user)
        except ValueError as exc:
            return _fail("invalid", str(exc))
        return JSONResponse({"success": True, "data": {**view(record), "secret": secret}, "errorCode": "", "errorMessage": ""})

    @app.post("/admin/tokens/batch")
    async def batch(request: Request, user: str = Header(alias="X-CCP-User")) -> JSONResponse:
        body = await request.json()
        admin = await _is_admin(app, user)
        result = app.state.store.batch(str(body.get("action")), list(body.get("ids") or []), user, admin)
        return JSONResponse({"success": True, "data": result, "errorCode": "", "errorMessage": ""})

    @app.delete("/admin/tokens/{token_id}")
    async def revoke_or_delete(token_id: str, request: Request, user: str = Header(alias="X-CCP-User")) -> JSONResponse:
        record = app.state.store.get(token_id)
        admin = await _is_admin(app, user)
        if record is None:
            return _fail("not_found", "令牌不存在", 404)
        if record.user != user and not admin:
            return _fail("forbidden", "只能操作自己的令牌", 403)
        if record.state == "active":
            app.state.store.revoke(record)
        else:
            app.state.store.remove(record)
        return JSONResponse({"success": True, "data": None, "errorCode": "", "errorMessage": ""})


async def _is_admin(app: FastAPI, user: str) -> bool:
    """问 ccp-backend：这个用户是不是平台管理员。问不到就当不是（fail-closed）。"""
    cfg = app.state.settings
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            resp = await client.get(
                f"{cfg.backend.rstrip('/')}/internal/whoami",
                params={"user": user},
                headers={"X-CCP-Internal-Key": cfg.internal_key},
            )
            if resp.status_code == 200:
                return bool(resp.json()["data"]["isPlatformAdmin"])
    except httpx.HTTPError:
        pass
    return False


# ------------------------------------------------------------------ JSON-RPC（MCP 客户端直连）


def _rpc_result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _register_rpc(app: FastAPI) -> None:
    from app.tools import TOOLS, run_tool

    @app.post("/mcp")
    async def mcp_rpc(request: Request) -> JSONResponse:
        record = app.state.store.resolve(_bearer(request.headers.get("Authorization")))
        if record is None:
            return JSONResponse(
                status_code=401,
                content={"success": False, "data": None, "errorCode": "unauthorized", "errorMessage": "MCP 令牌无效或已吊销", "showType": 0},
                headers={"WWW-Authenticate": 'Bearer realm="ccp-mcp"'},
            )
        try:
            message = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(_rpc_error(-32700, "Parse error"))

        method = message.get("method")
        request_id = message.get("id")
        if request_id is None and method and method.startswith("notifications/"):
            return JSONResponse(status_code=202, content=None)

        if method == "initialize":
            client_version = (message.get("params") or {}).get("protocolVersion")
            return JSONResponse(
                _rpc_result(
                    {
                        "protocolVersion": client_version or PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "ccp-mcp", "version": "1.0"},
                        "instructions": (
                            "算力云平台（CCP）：查资源 → 提交作业 → 看进度。"
                            "常用顺序 list_queues → list_projects → submit_job → get_job。"
                        ),
                    },
                    request_id,
                )
            )
        if method == "ping":
            return JSONResponse(_rpc_result({}, request_id))
        if method == "tools/list":
            return JSONResponse(_rpc_result({"tools": TOOLS}, request_id))
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            args = dict(params.get("arguments") or {})
            try:
                text = await run_tool(str(name), args, record.user, app.state.settings)
                return JSONResponse(
                    _rpc_result({"content": [{"type": "text", "text": text}]}, request_id)
                )
            except KeyError:
                return JSONResponse(_rpc_error(-32602, f"未知工具：{name}", request_id))
            except ToolError as exc:
                return JSONResponse(
                    _rpc_result(
                        {"content": [{"type": "text", "text": str(exc)}], "isError": True},
                        request_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _rpc_result(
                        {"content": [{"type": "text", "text": f"工具执行失败：{exc}"}], "isError": True},
                        request_id,
                    )
                )
        return JSONResponse(_rpc_error(-32601, f"Method not found: {method}", request_id))

    @app.get("/mcp")
    async def mcp_sse_unsupported() -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={"error": "本 MCP 服务器无状态，请用 POST（streamable HTTP 单应答模式）"},
        )


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


class ToolError(Exception):
    """业务校验失败：包成 isError=true 给 AI 看下一步怎么办。"""


def _register_tools(app: FastAPI) -> None:
    """就绪 / 存活探针（GET /healthz——/mcp 只认 POST，探针打 405 会误判）。"""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}


# JSON 输出统一 ensure_ascii=False（中文可读）
def _json_default(obj: Any) -> Any:  # pragma: no cover
    return str(obj)


json.dumps_ = json.dumps  # type: ignore[attr-defined]


def create_app_factory() -> FastAPI:
    """uvicorn --factory 入口（Dockerfile CMD 用）。"""
    return create_app()
