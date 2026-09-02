# Agent Orchestrator Backend

FastAPI service for the Agent Orchestrator platform: agent graph management, tool
definitions, RAG document uploads, agent execution (LangChain + LangGraph), and
per-user LLM provider settings. SQLite-backed, deployed as its own standalone
service alongside the other veeragenai backends.

## Structure

- `main.py` - app assembly: lifespan startup/shutdown, logging, CORS, health check
- `config.py` - environment-driven settings (JWT secret, frontend origins, cookie flags)
- `auth.py` - shared-auth JWT verification (`current_user_id`, `optional_user_id`)
- `database.py` - SQLite connection + schema (single shared DB across features)
- `models.py` - Pydantic request/response models
- `routers/` - one router per feature: `agents`, `tools`, `rag`, `execute`, `settings`
- `services/` - `llm_provider`, `orchestrator` (LangGraph agent graph), `tool_builder`,
  `builtin_tools` (the built-in tool catalogue and its implementations)
- `rag_storage.py` - Hugging Face bucket wrapper for original uploaded files
- `rag_vector_store.py` - Pinecone wrapper for chunk embeddings

## Shared storage (RAG)

RAG document uploads don't use local disk or a client-supplied API key for
storage. Instead, like `basicragapp`/`advancedragapp` in `veeragenai_projects_be`:

- The **original file** is uploaded to the same Hugging Face bucket
  veeragenai uses (`HUGGINGFACE_TOKEN`/`HUGGINGFACE_BUCKET`), under an
  `agent-orchestrator/users/{user_id}/documents/...` path so it can't collide
  with veeragenai's own paths.
- **Chunks and embeddings** are upserted into Pinecone using the same
  `PINECONE_API_KEY` (account) as veeragenai, but a **separate index**
  (`PINECONE_INDEX`, default `agent-orchestrator-rag`) — kept distinct from
  veeragenai's own index for isolation, even though both now use 768-dim
  Gemini embeddings.
- **Embeddings use Google Gemini only** (`rag_embeddings.py`, same
  `batchEmbedContents` call as `basicragapp/embeddings.py`), not OpenAI. The
  user picks the embedding model (`gemini-embedding-001` or
  `text-embedding-004`, both forced to 768-dim output) and supplies their own
  Gemini API key at upload time — it's used once and not persisted. Agents get
  a `rag` tool (`services/tool_builder.py::create_rag_tool`) that embeds the
  query with the user's *saved* `google_genai` key from `/settings/llm-configs`
  (the same key their Gemini-provider agents use) and searches their Pinecone
  namespace.

Get `HUGGINGFACE_TOKEN`, `HUGGINGFACE_BUCKET`, and `PINECONE_API_KEY` from
`veeragenai_projects_be`'s `.env` — see `.env.example` here for the exact
variable names.

When a document (or any other user data) passes the 48-hour retention window,
`database.cleanup_expired_data()` deletes its Pinecone vectors and Hugging
Face file *before* dropping the SQLite row, mirroring veeragenai's
`retention.py` ordering so nothing is orphaned in either store.

## Provider API keys

Keys are stored per user *per provider* in `llm_configs`, not per agent, so every
agent on the same provider shares one key. They can be entered in two places
that write the same record: Settings, or the API Key field on the agent form
(`api_key` on `POST /agents` and `PUT /agents/{id}`). Because an agent picks its
provider at creation time, entering the key there avoids creating an agent that
cannot run; `has_api_key` on every agent response tells the UI whether the
agent's provider is covered. Keys fall under the same 48-hour deletion as
everything else.

## Agent-to-agent delegation

A connection drawn from agent A to agent B makes B available to A as a tool
named `ask_<b_name>`. A's model decides which connected agents to consult, may
consult several, and composes their answers into its reply - so you chat with
one agent and get one answer back. `services/orchestrator.py` builds the target
agent's own graph on demand, meaning a delegate brings its own tools, prompt and
provider. Two guards keep this bounded: `MAX_DELEGATION_DEPTH` caps how many
hops one question may take, and the `chain` of already-visited agents is
filtered out of each delegate's options, so A -> B -> A cannot recurse. Every
hop is recorded and returned as `delegations` on the execute response.

## Built-in tools

`services/builtin_tools.py` holds the catalogue served by `GET /tools/builtin`
and the factory for each entry. Every tool is a plain REST call made with
aiohttp rather than a provider SDK, so adding one adds no dependency. A tool
whose required config is missing returns `None` from its factory and is simply
not given to the agent. Currently: date/time, calculator, Slack, Tavily,
Google (Serper), web page reader, generic HTTP request, GitHub, DuckDuckGo
and RAG search.

## Shared authentication

This backend does **not** run its own login system. It validates the same JWT
issued by [veeragenai_projects_be](../veeragenai_projects_be) (the auth backend),
read from either the `access_token` cookie (when the frontend proxies requests
same-origin, e.g. via a Vercel/Netlify rewrite) or an `Authorization: Bearer`
header. This is the same pattern `veeragenaiproject_be2` (Chunking Lab) uses.

**`JWT_SECRET` must be set to the exact same value as `JWT_SECRET` in
`veeragenai_projects_be`'s `.env`.** There is no separate signup/login here, and
no MongoDB dependency — the token's `sub` claim is trusted as the user id and
used to scope all agents/tools/executions/settings per user.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env
# edit .env: set JWT_SECRET to match veeragenai_projects_be
```

## Run

```bash
python main.py
# or
uvicorn main:app --reload --port 8003
```

Runs on port 8003 locally by default (8000 and 8001 are taken by
`veeragenai_projects_be` and `veeragenaiproject_be2`). Health check: `GET
/health` (no auth required). Every other route requires a valid JWT.

## Deploying (Vercel)

Same pattern as `veeragenaiproject_be2`:

1. Import this repo into Vercel as a Python project (zero-config: it detects
   `main.py`'s `app`).
2. Set `JWT_SECRET` (matching the auth backend), `FRONTEND_URL` (the deployed
   `veeragenai_projects_fe` URL), and `DATA_DIR=/tmp/agent_orchestrator`.
3. Deploy, then verify `https://<this-app>.vercel.app/health` returns
   `{"status":"ok"}`.
4. In `veeragenai_projects_fe`, add a rewrite (in `vercel.json` and
   `netlify.toml`) mapping `/agent-api/*` to this app's URL, so requests stay
   same-origin from the browser's perspective and the `access_token` cookie
   set by the auth backend is sent along automatically.

Vercel only permits runtime writes under `/tmp`, so with `DATA_DIR` unset (or
pointed at `/tmp`) agent/tool/execution data may not survive a cold start or
a request landing on a different instance. This is an acceptable trade-off
here since the app already auto-deletes all data after 48 hours by design.
For durable storage, either deploy to a host with a persistent disk (e.g.
Render) or migrate this backend's SQLite tables to MongoDB/Postgres.
