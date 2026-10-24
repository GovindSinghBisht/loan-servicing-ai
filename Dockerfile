# One image for the assistant API and the mock enterprise systems (docker-compose.yml runs each with its
# own command). Synthetic data is generated at build time; it is seeded, so every build has the same data.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY shared shared
COPY datagen datagen
COPY data/policies data/policies
COPY data/governance data/governance
COPY mock_systems mock_systems
COPY engine engine
RUN python -m datagen.generate \
    && useradd --create-home --uid 10001 app \
    && mkdir -p data/documents logs \
    && chown -R app:app data/documents logs data

USER app
EXPOSE 8000
CMD ["uvicorn", "engine.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
