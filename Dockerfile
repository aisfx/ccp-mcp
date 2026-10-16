FROM python:3.12-slim
WORKDIR /srv/ccp-mcp

# uv 同步依赖（锁版本进镜像）
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

COPY app ./app

EXPOSE 8020
CMD ["uv", "run", "uvicorn", "app.main:create_app_factory", "--factory", "--host", "0.0.0.0", "--port", "8020"]
