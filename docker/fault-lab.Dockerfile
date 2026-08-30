FROM ghcr.io/astral-sh/uv:0.11.25 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "opsswarm.fault_lab.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
