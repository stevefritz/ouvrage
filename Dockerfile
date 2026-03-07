FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache "mcp[cli]>=1.2.0" "aiosqlite>=0.20.0" "uvicorn>=0.34.0" "starlette>=0.45.0" "sse-starlette>=2.2.0"

COPY database.py server.py ./

EXPOSE 8100

CMD ["python", "server.py"]
