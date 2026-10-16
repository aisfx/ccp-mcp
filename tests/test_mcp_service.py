"""ccp-mcp 单元/接口测试：令牌生命周期、JSON-RPC 协议、工具经 HTTP 调平台。"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.store import TokenStore

INTERNAL = {"X-CCP-Internal-Key": "test-key"}


@pytest.fixture()
def app():
    cfg = Settings(internal_key="test-key", backend="http://backend.test", data_path="")
    return create_app(cfg, TokenStore())


@pytest.fixture()
def client(app):
    return TestClient(app)


def _issue(client, name="cursor", user="sunfx"):
    resp = client.post(
        "/admin/tokens", json={"name": name}, headers={**INTERNAL, "X-CCP-User": user}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ 令牌管理


def test_token_lifecycle(client):
    created = _issue(client)
    assert created["secret"].startswith("ccp-mcp-")
    assert created["state"] == "active"

    listed = client.get("/admin/tokens", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    items = listed.json()["data"]["list"]
    assert [t["id"] for t in items] == [created["id"]]
    # 视图里没有明文 / 哈希
    assert "secret" not in items[0] and "secret_hash" not in items[0]

    # 吊销 → 删除
    client.delete(f"/admin/tokens/{created['id']}", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    listed = client.get("/admin/tokens", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    assert listed.json()["data"]["list"][0]["state"] == "revoked"
    client.delete(f"/admin/tokens/{created['id']}", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    assert client.get("/admin/tokens", headers={**INTERNAL, "X-CCP-User": "sunfx"}).json()["data"]["total"] == 0


def test_admin_gate_and_isolation(client, app):
    _issue(client, "mine", "sunfx")
    _issue(client, "wangs", "wang")
    # 普通用户只看自己的
    mine = client.get("/admin/tokens", headers={**INTERNAL, "X-CCP-User": "sunfx"}).json()["data"]["list"]
    assert [t["name"] for t in mine] == ["mine"]
    # 没有 internal key 一律 401
    assert client.get("/admin/tokens", headers={"X-CCP-User": "sunfx"}).status_code == 401
    # 不能动别人的
    wangs = client.get("/admin/tokens", headers={**INTERNAL, "X-CCP-User": "wang"}).json()["data"]["list"][0]
    resp = client.delete(f"/admin/tokens/{wangs['id']}", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    assert resp.status_code == 403


def test_batch_semantics(client):
    a = _issue(client, "a")
    b = _issue(client, "b")
    client.delete(f"/admin/tokens/{b['id']}", headers={**INTERNAL, "X-CCP-User": "sunfx"})  # b 吊销
    result = client.post(
        "/admin/tokens/batch",
        json={"action": "revoke", "ids": [a["id"], b["id"], "mt-none"]},
        headers={**INTERNAL, "X-CCP-User": "sunfx"},
    ).json()["data"]
    assert result["applied"] == [a["id"]]
    assert result["skipped"] == [b["id"]]
    assert result["missing"] == ["mt-none"]


# ------------------------------------------------------------------ JSON-RPC


def _rpc(client, token, method, params=None, request_id=1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_rpc_auth(client):
    created = _issue(client)
    # 没令牌 / 坏令牌 401
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    bad = _rpc(client, "ccp-mcp-bogus", "ping")
    assert bad.status_code == 401
    # 对的令牌 ping 通
    ok = _rpc(client, created["secret"], "ping")
    assert ok.json()["result"] == {}
    # 吊销后立刻 401
    client.delete(f"/admin/tokens/{created['id']}", headers={**INTERNAL, "X-CCP-User": "sunfx"})
    assert _rpc(client, created["secret"], "ping").status_code == 401


def test_rpc_initialize_and_list(client):
    created = _issue(client)
    init = _rpc(client, created["secret"], "initialize", {"protocolVersion": "2025-03-26"})
    assert init.json()["result"]["serverInfo"]["name"] == "ccp-mcp"
    listed = _rpc(client, created["secret"], "tools/list")
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert "submit_job" in names and "list_queues" in names
    # 未知方法 -32601；notification 202
    assert _rpc(client, created["secret"], "nope").json()["error"]["code"] == -32601
    notif = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {created['secret']}"},
    )
    assert notif.status_code == 202
    assert client.get("/mcp").status_code == 405


@respx.mock
def test_tool_call_via_backend(app, client):
    """工具调用：铸 session → 调 /api/v1/*，业务错包 isError。"""
    created = _issue(client, user="sunfx")
    base = "http://backend.test"

    mint = respx.post(f"{base}/internal/sessions").respond(
        200, json={"success": True, "data": {"token": "sess-1", "expiresIn": 300}}
    )
    respx.get(f"{base}/api/v1/queues").respond(
        200,
        json={
            "success": True,
            "data": {
                "list": [
                    {
                        "name": "ai-train",
                        "computeClass": "smart",
                        "gpuModels": ["NVIDIA A100-SXM4-80GB"],
                        "capability": {"cpu": 192, "nvidia.com/gpu": 8},
                        "allocated": {"cpu": 64, "nvidia.com/gpu": 2},
                        "description": "大模型训练",
                    }
                ],
                "total": 1,
            },
        },
    )

    resp = _rpc(client, created["secret"], "tools/call", {"name": "list_queues", "arguments": {}})
    body = resp.json()["result"]
    assert body["isError"] is not True if "isError" in body else True
    text = body["content"][0]["text"]
    assert "ai-train" in text and "A100" in text and "64/192" in text
    # 铸 session 带的是属主
    assert mint.calls.last.request.headers["X-CCP-Internal-Key"] == "test-key"
    mint_json = eval(mint.calls.last.request.content) if isinstance(mint.calls.last.request.content, bytes) else None
    assert mint_json == {"user": "sunfx"}


@respx.mock
def test_tool_business_error_is_error(client):
    created = _issue(client)
    base = "http://backend.test"
    respx.post(f"{base}/internal/sessions").respond(
        200, json={"success": True, "data": {"token": "sess-1", "expiresIn": 300}}
    )
    respx.get(f"{base}/api/v1/queues").respond(
        200, json={"success": False, "data": None, "errorCode": "billing_paused", "errorMessage": "租户计费已暂停"}
    )
    resp = _rpc(client, created["secret"], "tools/call", {"name": "list_queues"})
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "租户计费已暂停" in result["content"][0]["text"]


# ------------------------------------------------------------------ 两副本同步


def test_two_replicas_share_file(tmp_path):
    """副本 B 在 A 写过之后 reload_if_stale 能看到新令牌（NFS 同一份文件）。"""
    path = str(tmp_path / "tokens.json")
    cfg = Settings(internal_key="test-key", backend="http://backend.test", data_path=path)
    from app.main import create_app as _create

    a = TestClient(_create(cfg, TokenStore(path)))
    b = TestClient(_create(cfg, TokenStore(path)))
    h = {**INTERNAL, "X-CCP-User": "sunfx"}

    created = a.post("/admin/tokens", json={"name": "by-a"}, headers=h).json()["data"]
    # B 副本没 reload 前看不到；请求进来（gate 里 reload_if_stale）就看到了
    assert b.get("/admin/tokens", headers=h).json()["data"]["total"] == 1
    assert b.delete(f"/admin/tokens/{created['id']}", headers=h).status_code == 200
    # /mcp 认证同样先 reload：A 再发一枚，B 上立刻能用
    second = a.post("/admin/tokens", json={"name": "by-a-2"}, headers=h).json()["data"]
    ok = b.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {second['secret']}"},
    )
    assert ok.json()["result"] == {}
