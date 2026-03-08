FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache "mcp[cli]>=1.2.0" "aiosqlite>=0.20.0" "uvicorn>=0.34.0"

COPY database.py server.py ./

RUN groupadd -g 1000 switchboard && \
    useradd -r -u 1000 -g switchboard -s /bin/false switchboard && \
    mkdir -p /data && chown switchboard:switchboard /data

USER switchboard

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/health')" || exit 1

CMD ["python", "server.py"]
