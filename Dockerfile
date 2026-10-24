FROM node:24-slim AS widget
WORKDIR /widget
COPY widget/package.json widget/package-lock.json ./
RUN npm ci
COPY widget ./
# vite writes to ../src/shopdesk/static/widget
RUN mkdir -p /src/shopdesk/static && npm run build

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=widget /src/shopdesk/static/widget ./src/shopdesk/static/widget
RUN uv sync --frozen --no-dev
# Bake the BM25 model into the image so pods don't download it at startup.
RUN .venv/bin/python -c "from fastembed import SparseTextEmbedding; \
SparseTextEmbedding('Qdrant/bm25', cache_dir='/app/models')"

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app /app /app
ENV PATH=/app/.venv/bin:$PATH FASTEMBED_CACHE_PATH=/app/models PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "shopdesk.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
