"""MCP 令牌库（ccp-mcp 自有，独立于 ccp-backend 的 store）：JSON 文件落盘。

为什么不共用 ccp 的 store：那是单体内存库 + 请求前后 save/reload 的两副本同步协议，
两个进程共写同一个文件只会互相踩。令牌本来就是给独立进程认证用的，自己一份。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

TOKEN_PREFIX = "ccp-mcp-"
TOKEN_LIMIT = 8  # 每人最多几枚有效，防囤积


def now() -> datetime:
    return datetime.now(timezone.utc)


class TokenRecord(BaseModel):
    id: str
    name: str
    user: str
    secret_hash: str  # sha256，明文不落库
    prefix: str  # 前 12 位认脸
    state: str = "active"  # active / revoked
    created_at: datetime
    last_used_at: datetime | None = None


class TokenStore:
    """线程安全的极简 JSON 存储：写时整体落盘，启动时读回。

    两副本共一份 NFS：读前对 mtime，文件被另一个副本写过就重装（同 ccp-backend
    的 reload_if_stale 协议）；写后落盘 + 更新自己的 mtime 快照。
    """

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._seen_mtime: float | None = None
        self.tokens: list[TokenRecord] = []
        self.reload_if_stale(force=True)

    def reload_if_stale(self, force: bool = False) -> bool:
        """文件比内存新就重装；返回是否重装了。没有文件路径（测试）时永远 False。"""
        if self._path is None:
            return False
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self._seen_mtime:
            return False
        try:
            raw = json.loads(self._path.read_text())
            tokens = [TokenRecord(**item) for item in raw]
        except (OSError, ValueError):
            return False  # 另一副本写到一半：拿旧内存态，下一个请求再试
        self.tokens = tokens
        self._seen_mtime = mtime
        return True

    # ------------------------------------------------------------------ 基础

    def _flush(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                [t.model_dump(mode="json") for t in self.tokens],
                ensure_ascii=False,
                indent=2,
            )
        )
        tmp.replace(self._path)
        self._seen_mtime = self._path.stat().st_mtime

    def _hash(secret: str) -> str:  # noqa: N805 - 与 ccp 侧同一写法
        return hashlib.sha256(secret.encode()).hexdigest()

    # ------------------------------------------------------------------ CRUD

    def issue(self, name: str, user: str) -> tuple[TokenRecord, str]:
        """发一枚：返回（记录, 明文）——明文只在这一刻存在。"""
        mine = [t for t in self.tokens if t.user == user and t.state == "active"]
        if len(mine) >= TOKEN_LIMIT:
            raise ValueError(f"每人最多 {TOKEN_LIMIT} 枚有效令牌，先吊销一枚再建")
        key = name.strip()
        if not key:
            raise ValueError("令牌要有个名字")
        secret = f"{TOKEN_PREFIX}{secrets.token_hex(16)}"
        record = TokenRecord(
            id=f"mt-{secrets.token_hex(4)}",
            name=key,
            user=user,
            secret_hash=TokenStore._hash(secret),
            prefix=secret[:12],
            created_at=now(),
        )
        with self._lock:
            self.tokens.insert(0, record)
            self._flush()
        return record, secret

    def get(self, token_id: str) -> TokenRecord | None:
        return next((t for t in self.tokens if t.id == token_id), None)

    def revoke(self, record: TokenRecord) -> None:
        with self._lock:
            record.state = "revoked"
            self._flush()

    def remove(self, record: TokenRecord) -> None:
        with self._lock:
            self.tokens = [t for t in self.tokens if t.id != record.id]
            self._flush()

    def list_for(self, user: str, is_admin: bool) -> list[TokenRecord]:
        if is_admin:
            return list(self.tokens)
        return [t for t in self.tokens if t.user == user]

    def batch(self, action: str, ids: list[str], user: str, is_admin: bool) -> dict[str, list[str]]:
        """批量吊销 / 删除，语义同 ccp 的 serving.batch_keys。"""
        applied: list[str] = []
        skipped: list[str] = []
        missing: list[str] = []
        rejected: list[str] = []
        with self._lock:
            for token_id in dict.fromkeys(ids):
                record = self.get(token_id)
                if record is None:
                    missing.append(token_id)
                elif record.user != user and not is_admin:
                    rejected.append(token_id)
                elif action == "revoke" and record.state == "active":
                    record.state = "revoked"
                    applied.append(token_id)
                elif action == "purge" and record.state == "revoked":
                    self.tokens = [t for t in self.tokens if t.id != token_id]
                    applied.append(token_id)
                else:
                    (skipped if action == "revoke" else rejected).append(token_id)
            self._flush()
        return {"applied": applied, "skipped": skipped, "missing": missing, "rejected": rejected}

    # ------------------------------------------------------------------ 认证

    def resolve(self, secret: str | None) -> TokenRecord | None:
        """Bearer → 令牌记录。顺手记 last_used_at（不落盘也行，重新认证会再写）。"""
        if not secret or not secret.startswith(TOKEN_PREFIX):
            return None
        digest = TokenStore._hash(secret)
        record = next(
            (t for t in self.tokens if t.state == "active" and t.secret_hash == digest), None
        )
        if record is not None:
            record.last_used_at = record.last_used_at or now()
        return record


def view(record: TokenRecord) -> dict[str, Any]:
    """给前端的形态（camelCase，无哈希）。"""
    return {
        "id": record.id,
        "name": record.name,
        "prefix": record.prefix,
        "state": record.state,
        "createdAt": record.created_at.isoformat(),
        "lastUsedAt": record.last_used_at.isoformat() if record.last_used_at else None,
    }
