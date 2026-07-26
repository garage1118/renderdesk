FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["uvicorn", "renderdesk.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
