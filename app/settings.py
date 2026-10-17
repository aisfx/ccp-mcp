"""配置：全部走环境变量（前缀 CCP_MCP_），零配置也能起（fake 后端联调用）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CCP_MCP_", env_file=".env", extra="ignore")

    # ccp-backend 的基址（集群内：http://ccp-backend.ccp-system.svc:8010）
    backend: str = "http://127.0.0.1:8010"
    # 服务互信：与 ccp-backend 的 CCP_INTERNAL_KEY 相同才放行内部端点
    internal_key: str = ""
    # 令牌库三选一（2026-10-18 用户「把数据全部都存储到 pg 数据库上面去」）：
    #   database_url：生产。PG（与 ccp-backend 同一个库，另一张表）；多副本不再丢更新
    #   data_path：JSON 文件（开发/单副本）。两者都配时 PG 优先
    #   都空：纯内存（联调 / 测试）
    database_url: str | None = None
    data_path: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
