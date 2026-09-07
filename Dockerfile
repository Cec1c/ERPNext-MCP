FROM python:3.12.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

EXPOSE 8089

ENTRYPOINT ["/app/.venv/bin/python", "-u", "-m", "erpnext_mcp"]
