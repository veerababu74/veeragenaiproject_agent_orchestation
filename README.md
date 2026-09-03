# Agent Orchestrator Backend

> **Documentation**
> - [ARCHITECTURE.md](ARCHITECTURE.md) — what the system is: modules, data model, API reference, design decisions.
> - [CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md) — how the code works: every function line by line, and why it is written that way.
> - [BLOG.md](BLOG.md) — the narrative version, including the bugs that shaped the design.

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

## Orchestration modes

Each agent has an `orchestration_mode` deciding how it uses the agents connected
to it. **In every mode except `supervisor`, the agent you chat with is the one
that writes the final answer** - the mode only decides how its connected agents
contribute first. That is deliberate: it means the direction you drew an arrow
in never changes the outcome, which is what made direction-sensitive routing so
confusing before.

| Mode | Behaviour |
|---|---|
| `supervisor` (default) | Connected agents are offered to the model as `ask_<name>` tools and it decides which to call, if any. Flexible, not repeatable. |
| `sequential` | Every connected agent runs in turn, each one shown what the previous ones answered, then this agent composes. Deterministic. |
| `parallel` | All connected agents are asked concurrently, then this agent merges their answers. Best when the sub-questions are independent. |
| `conditional` | Only connected agents whose condition matches the question run, then sequentially as above. |

A connection's condition is its `condition_expr`, falling back to its `label` -
so the single "when should this agent be consulted?" prompt in the UI serves
both as the supervisor's routing hint and as the conditional rule. Conditions
are matched in one classification call covering every edge, not one per edge,
and an unusable reply falls open so a routing hiccup cannot leave the user with
no answer.

Modes apply to the agent you chat with. Agents it consults run in the ordinary
supervisor way, which keeps a deep graph from multiplying out of control.

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

## Conversation memory

`POST /execute` and `POST /execute/stream` accept a `conversation_id`. When one
is given, the last `HISTORY_LIMIT` turns of that thread are replayed to the
agent and the new turn is appended, so the agent remembers earlier messages.
Omit it for a one-shot run that stores nothing. History is scoped per user and
per conversation, is deleted by the same 48-hour sweep, and a failed run is not
recorded. `GET`/`DELETE /execute/conversations/{id}` read and clear a thread.

## Streaming

`POST /execute/stream` returns server-sent events, each a JSON object with a
`type`: `start`, `token` (a chunk of the reply), `delegation` (a question sent
to a connected agent, or its answer), then `done` or `error`. Only the agent
being chatted with streams tokens - it is tagged `primary_agent` so the models
of agents it delegates to, which run their own graphs underneath, do not
interleave. Delegation steps are pushed onto a queue by the tool as they
happen, so the client sees which agent is being consulted mid-run.

## Tracing and observability

Every run records an ordered timeline in `run_events`: the question, what the
model reasoned and which tools it chose off the back of that reasoning, each
tool call with its arguments, each result or failure with its duration, and the
final answer.

`services/tracing.py` does this with a LangChain callback handler rather than
by instrumenting the orchestrator by hand, because callbacks propagate into the
graphs that delegated agents run - a sub-agent's own reasoning and tool calls
land in the same timeline, in the right order, with no extra plumbing. Each
compiled graph is stamped with `agent_name`/`agent_id`/`agent_depth`, so every
event says which agent produced it and how deep the delegation went. Events are
buffered during the run and written in one transaction at the end; token usage
is summed from the models' `usage_metadata` into `agent_executions.tokens_used`.

- `GET /execute/runs/{execution_id}` - one run, its full timeline, and a summary
  (steps, tool calls, tools used, agents involved, delegations, depth, errors).
- `GET /execute/metrics` - success rate, average and p95 duration, total tokens,
  and per-tool and per-agent breakdowns.
- `POST /execute/stream` also emits each step live as a `trace` event, so the UI
  shows what the agent is doing while it is still doing it.

Traces fall under the same 48-hour deletion as everything else.

## Export and import

`GET /agents/export` returns the whole workspace - agents, connections, tools,
custom schemas and tool assignments - as JSON, referencing agents and tools by
index so `POST /agents/import` can recreate them with fresh ids. Because
everything is deleted after 48 hours, this is how a workspace survives.
**Credentials are stripped on export** (anything keyed `api_key`, `token`,
`webhook_url`, `password`, and friends) since the file is downloaded and often
shared; they must be re-entered after importing.

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
