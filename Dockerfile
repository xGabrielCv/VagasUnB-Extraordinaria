FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/postgresql.asc \
    && echo 'deb [signed-by=/usr/share/keyrings/postgresql.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE THIRD_PARTY_NOTICES ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev && mkdir -p /app/.private && chown -R app:app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER app
STOPSIGNAL SIGTERM
CMD ["uvicorn", "vagasunb.admin:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]

FROM runtime AS tests
COPY --chown=app:app tests ./tests
COPY --chown=app:app compose.yaml .dockerignore ./
RUN uv sync --frozen
CMD ["pytest", "-q", "--disable-socket", "--allow-hosts=postgres"]

FROM runtime AS production
