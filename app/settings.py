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
    # 令牌库：JSON 文件路径（PVC 挂载点）；空 = 纯内存（联调 / 测试）
    data_path: str = ""
    # 服务端口
    port: int = 8020


@lru_cache
def get_settings() -> Settings:
    return Settings()
