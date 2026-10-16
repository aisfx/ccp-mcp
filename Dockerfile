FROM python:3.12-slim
WORKDIR /srv/ccp-mcp

# 依赖少（fastapi/uvicorn/httpx/pydantic-settings），直接 pip 装进镜像
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" "pydantic>=2.7" "pydantic-settings>=2.2"

COPY app ./app

EXPOSE 8020
CMD ["python", "-m", "uvicorn", "app.main:create_app_factory", "--factory", "--host", "0.0.0.0", "--port", "8020"]
