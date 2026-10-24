# Running shopdesk

Three ways to run it, from quickest to production-ready.

- [1. Docker Compose (recommended)](#1-docker-compose-recommended)
- [2. Local development (uv)](#2-local-development-uv)
- [3. Production](#3-production)
- [Connecting the outside world](#connecting-the-outside-world)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)

## What you need

| | Needed for | Where to get it |
|---|---|---|
| **OpenAI API key** | the agent, the injection screen, embeddings | platform.openai.com → API keys |
| **Qdrant** URL + API key | the help-centre index | cloud.qdrant.io (an EU region), or a local container |
| Langfuse keys | tracing and eval scores (optional) | cloud.langfuse.com (EU) |
| Zendesk OAuth client | real ticket handoff (optional; a local sink is used otherwise) | Admin Center → APIs → OAuth clients |
| Anthropic API key | the fallback model (optional) | console.anthropic.com |
| Shop API URL + key | live orders (optional; demo orders otherwise) | your shop backend |

Copy `.env.example` to `.env` and fill in what you have. Everything optional degrades
gracefully.

---

## 1. Docker Compose (recommended)

```bash
cp .env.example .env            # add OPENAI_API_KEY, QDRANT_URL, QDRANT_API_KEY
docker compose up -d --build    # app + replay worker + retention job, SQLite on a volume
docker compose run --rm app python -m shopdesk.rag.ingest --markdown kb
```

Open **http://localhost:8000/**. With `ENV=dev` you get the demo storefront and the widget;
sign in as Alex to try order lookups and handoffs.

```mermaid
flowchart LR
    subgraph compose["docker compose"]
        app["app :8000"]
        replay["replay worker"]
        retention["retention job"]
        vol[("volume: shopdesk-data<br/>SQLite")]
        pg[("postgres<br/>profile: postgres")]
    end
    app --- vol
    replay --- vol
    retention --- vol
    app -.-> pg
```

| Command | What it does |
|---|---|
| `docker compose up -d --build` | build the image (widget + Python) and start everything |
| `docker compose logs -f app` | follow the app logs |
| `docker compose run --rm app python -m shopdesk.rag.ingest --markdown kb` | index the help centre (the image ships the sample articles in `kb/`) |
| `docker compose down` | stop (data stays in the volume) |
| `docker compose down -v` | stop **and delete** all conversations and stored tokens |

**Using Postgres instead of SQLite:**

```bash
COMPOSE_DATABASE_URL=postgresql://shopdesk:shopdesk@postgres:5432/shopdesk \
  docker compose --profile postgres up -d --build
```

**Your own help centre:** mount a folder of Markdown files and ingest it:

```bash
docker compose run --rm -v "$PWD/my-help-centre:/app/kb-custom:ro" app \
  python -m shopdesk.rag.ingest --markdown kb-custom
```

Each file can start with front matter:

```markdown
---
id: kb-returns
title: Returns and refunds
url: https://help.example.com/returns
updated: 2026-09-01
---
# Returns and refunds
...
```

---

## 2. Local development (uv)

```bash
uv sync                                               # Python 3.12 deps
(cd widget && npm ci && npm run build)                # chat widget → src/shopdesk/static/widget
cp .env.example .env
uv run python -m shopdesk.rag.ingest --markdown data/kb
uv run python -m shopdesk.devserver                   # http://localhost:8000
```

`shopdesk.devserver` is plain uvicorn, except on Windows, where it switches to a selector
event loop (psycopg's async driver can't use the default Proactor loop).

| Task | Command |
|---|---|
| Tests | `uv run pytest --cov` |
| Lint + types | `uv run ruff check src tests evals && uv run mypy src` |
| Widget tests | `cd widget && npm test` |
| Widget dev loop | `cd widget && npm run build` after a change, then reload the page |
| Live evals | `uv run python -m evals.run` (uses real models and Qdrant) |
| One eval | `uv run python -m evals.run --only returns-window` |
| Postgres tests | `docker compose --profile postgres up -d postgres`, then `TEST_DATABASE_URL=postgresql://shopdesk:shopdesk@localhost:5433/shopdesk uv run pytest` |

---

## 3. Production

### Checklist

- [ ] `ENV=prod`. The app refuses to start without its secrets, and the `/dev/*` routes
  disappear.
- [ ] Generate secrets:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # TOKEN_ENCRYPTION_KEY
  python -c "import secrets; print(secrets.token_hex(32))"                                    # PSEUDONYM_KEY
  python -c "import secrets; print(secrets.token_urlsafe(32))"                                # WIDGET_JWT_SECRET, ADMIN_TOKEN
  ```
- [ ] `WIDGET_JWT_SECRET` shared with the shop backend, which signs shopper tokens (HS256;
  `sub`, `email`, `given_name`, `aud=shopdesk-widget`, `exp` ≤ 15 min).
- [ ] `DATABASE_URL`: SQLite on a persistent volume for one server, Postgres for more.
- [ ] HTTPS in front (load balancer or reverse proxy); `CORS_ORIGINS=["https://your-shop"]`.
- [ ] Zendesk OAuth redirect URL set to `https://<host>/v1/zendesk/oauth/callback`, then
  connect once via `/v1/zendesk/oauth/start`.
- [ ] Zendesk webhook + trigger for handback (see below), or `ZENDESK_HANDBACK_POLL_SECONDS=30`.
- [ ] EU region for everything; `OPENAI_BASE_URL=https://eu.api.openai.com/v1` if your
  OpenAI project has EU data residency.
- [ ] Ingest the real help centre, run `python -m evals.run` against it, and check Langfuse.

### Scaling out

1. Switch `DATABASE_URL` to Postgres.
2. Run several `app` replicas behind a load balancer with **sticky routing on the
   `conversation_id`** (body field). Per-conversation locks are in-process, so one
   conversation must stay on one replica.
3. Run as many `replay` workers as you like; dead letters are claimed with leases.
4. Keep the handback poller on **one** replica (set `ZENDESK_HANDBACK_POLL_SECONDS=0` on
   the others), or rely on the webhook.

### Embedding the widget

```html
<script src="https://support-api.example.com/widget/shopdesk-widget.js"
        data-api-base="https://support-api.example.com"
        data-token-url="/account/support-token"
        data-title="Northwind support" data-accent="#1f4d3f" defer></script>
```

`data-token-url` is an endpoint on the shop that returns `{"token": "<jwt>"}` for a
signed-in shopper or `{"token": null}` for guests. Other options: `data-font="inherit"`
(use the shop's font) and `data-greeting`.

---

## Connecting the outside world

### Zendesk

1. **OAuth client:** Admin Center → Apps and integrations → **APIs → OAuth clients** →
   Add. Kind *Confidential*; redirect URL `https://<host>/v1/zendesk/oauth/callback`;
   scopes `tickets:read tickets:write users:read users:write`. Copy the identifier and
   secret into `ZENDESK_CLIENT_ID` / `ZENDESK_CLIENT_SECRET`.
2. **Connect:** open `https://<host>/v1/zendesk/oauth/start`, sign in with `admin` /
   `ADMIN_TOKEN`, and click **Allow**. `GET /v1/admin/zendesk/status` shows the scopes and
   expiry.
3. **Handback webhook:** Admin Center → Webhooks → create one for
   `https://<host>/v1/zendesk/webhook` (POST, JSON) and copy its signing secret to
   `ZENDESK_WEBHOOK_SECRET`. Then add a trigger with conditions *Status is Solved* and
   *Tags contains `shopdesk`*, action *Notify webhook*, with body:
   ```json
   {"ticket_id": {{ticket.id}}, "external_id": "{{ticket.external_id}}", "status": "{{ticket.status}}"}
   ```

### Shop API

Implement three read-only endpoints (bearer `COMMERCE_API_KEY`):
`GET /customers/{id}/orders/{order_id}`, `GET /customers/{id}/orders?limit=5`,
`GET /customers/{id}`. The response models are in `src/shopdesk/integrations/commerce.py`.
Set `COMMERCE_API_URL`; without it the demo orders are used.

---

## Operations

| What | How |
|---|---|
| Health | `GET /healthz`; the Docker image has a built-in `HEALTHCHECK` |
| Zendesk connection | `GET /v1/admin/zendesk/status` (basic auth) |
| Dead letters | `GET /v1/admin/dead-letters`; force a replay with `POST /v1/admin/dead-letters/replay` |
| Erase a conversation | `DELETE /v1/conversations/{id}` with the shopper's token |
| Retention | runs daily in Compose; elsewhere schedule `python -m shopdesk.retention` (cron, k8s CronJob) |
| Re-index | re-run ingest; unchanged articles are skipped, changed ones swapped atomically |
| Switch embedding model | set a **new** `QDRANT_COLLECTION` and re-ingest (the app refuses a dimension mismatch) |
| Traces | Langfuse → Traces; sessions are pseudonymous thread ids |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `invalid_api_key` from OpenAI | key truncated when pasted (project keys are 164 chars) or revoked |
| Model not found | check the exact model ids your key has: `curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"` |
| `Psycopg cannot use the 'ProactorEventLoop'` (Windows) | start with `python -m shopdesk.devserver`, not `uvicorn` directly |
| App exits: `ZENDESK_AUTH_MODE=oauth needs ZENDESK_CLIENT_ID` | subdomain set without an OAuth client; add the client or unset `ZENDESK_SUBDOMAIN` |
| Zendesk "connected: false" after a restart | `DATABASE_URL` unset, so tokens were in memory; set it and connect once more |
| `collection … has dense size …` | embedding settings changed; use a new `QDRANT_COLLECTION` and re-ingest |
| Widget not showing | build it (`cd widget && npm run build`) or use the Docker image, which builds it |
| Every answer is "I don't have that information" | the help centre isn't ingested into the collection the app uses (`QDRANT_COLLECTION`) |
| Handback never happens locally | Zendesk can't reach `localhost`; set `ZENDESK_HANDBACK_POLL_SECONDS=15` |
