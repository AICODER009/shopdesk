# --- 1. Chat widget (React → one self-contained JS bundle) ---------------------------------
FROM node:24-slim AS widget
WORKDIR /widget
COPY widget/package.json widget/package-lock.json ./
RUN npm ci
COPY widget ./
# vite writes to ../src/shopdesk/static/widget
RUN mkdir -p /src/shopdesk/static && npm run build

# --- 2. Python dependencies + app -----------------------------------------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=widget /src/shopdesk/static/widget ./src/shopdesk/static/widget
RUN uv sync --frozen --no-dev
# Bake the BM25 model into the image so containers don't download it at startup.
RUN .venv/bin/python -c "from fastembed import SparseTextEmbedding; \
SparseTextEmbedding('Qdrant/bm25', cache_dir='/app/models')"

# --- 3. Runtime -----------------------------------------------------------------------------
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app /app /app
COPY --chown=app data/kb ./kb
# SQLite lives here; mount a volume on it. Created up front so the volume inherits the owner.
RUN mkdir -p /app/data && chown app /app/data
ENV PATH=/app/.venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/app/models \
    PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "shopdesk.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
