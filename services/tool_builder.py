from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model
from typing import Any, Type
from urllib.parse import quote
import json, logging, re, aiohttp

logger = logging.getLogger('agent-orchestrator.tools')

JSON_TYPES = {'number': float, 'integer': int, 'boolean': bool, 'array': list, 'object': dict, 'string': str}

def build_pydantic_schema(schema_dict, schema_name='ToolInput'):
    """Turn a stored JSON schema into the args model for a custom tool.

    Must be create_model, not type(): `name: (type, FieldInfo)` is create_model's
    signature, and passing it to type() leaves the fields unannotated, which
    pydantic v2 rejects outright - so every custom tool with a field failed to
    build, taking down any agent it was assigned to.
    """
    fields = {}
    for key, val in (schema_dict or {}).get('properties', {}).items():
        if not isinstance(key, str) or not key.isidentifier():
            continue  # not addressable as a python kwarg
        field_type = JSON_TYPES.get(val.get('type', 'string'), str)
        default = val.get('default', ...)
        description = val.get('description', '') or ''
        fields[key] = (field_type, Field(description=description) if default is Ellipsis
                       else Field(default=default, description=description))
    safe_name = re.sub(r'\W|^(?=\d)', '_', schema_name) or 'ToolInput'
    return create_model(safe_name, **fields)

async def execute_custom_tool(api_url, method, headers, auth_type, auth_config, request_body):
    method = (method or 'POST').upper()
    if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        return json.dumps({'error': f'Unsupported method: {method}'})
    args = dict(request_body or {})
    req_headers = {**headers}
    if auth_type == 'bearer':
        req_headers['Authorization'] = f"Bearer {auth_config.get('token', '')}"
    elif auth_type == 'api_key':
        req_headers[auth_config.get('header_name', 'X-API-Key')] = auth_config.get('api_key', '')
    elif auth_type == 'basic':
        import base64
        cred = base64.b64encode(f"{auth_config.get('username','')}:{auth_config.get('password','')}".encode()).decode()
        req_headers['Authorization'] = f'Basic {cred}'

    # A {placeholder} in the URL is filled from the arguments and then consumed,
    # so it is not also sent as a query string or body field.
    url = api_url
    for key in list(args):
        token = '{' + key + '}'
        if token in url:
            url = url.replace(token, quote(str(args.pop(key)), safe=''))

    try:
        async with aiohttp.ClientSession() as session:
            kw = {'headers': req_headers, 'timeout': aiohttp.ClientTimeout(total=30)}
            if method in {'GET', 'DELETE'}:
                # These carry no body, so the agent's arguments belong in the
                # query string - previously they were dropped entirely.
                kw['params'] = {k: str(v) for k, v in args.items() if v is not None}
            else:
                kw['json'] = args
                kw['headers'] = {**req_headers, 'Content-Type': 'application/json'}
            async with session.request(method, url, **kw) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    return json.dumps({'error': f'HTTP {resp.status}', 'body': body[:1000]})
                try:
                    return json.dumps(json.loads(body), indent=2)
                except ValueError:
                    return body[:6000]  # not every API answers with JSON
    except Exception as e:
        return json.dumps({'error': str(e)})

def tool_api_name(tool_name, fallback='custom_tool'):
    """Providers only accept [a-zA-Z0-9_-] for a tool name, but users type
    things like "My API", so normalise rather than let the call be rejected."""
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '_', (tool_name or '').strip()).strip('_')
    return (slug[:64] or fallback)

def create_custom_tool(tool_name, tool_description, schema, cts):
    name = tool_api_name(tool_name)
    InputModel = build_pydantic_schema(schema, f'{name}_Input')
    async def _run(**kwargs):
        return await execute_custom_tool(cts['api_url'], cts['method'], cts.get('headers',{}), cts.get('auth_type','none'), cts.get('auth_config',{}), kwargs)
    return StructuredTool.from_function(coroutine=_run, name=name,
                                        description=tool_description or f'Call the {tool_name} API.',
                                        args_schema=InputModel)

def _user_embedding_model(user_id):
    """The model the user's documents were actually embedded with.

    A query vector is only comparable to document vectors from the same model,
    and the upload form lets the user choose, so querying with the default
    would silently mismatch whenever they picked the other one.
    """
    from database import get_db
    from rag_embeddings import DEFAULT_EMBEDDING_MODEL
    conn = get_db()
    row = conn.execute(
        '''SELECT embedding_model FROM rag_documents
           WHERE user_id=? AND status='ready' AND embedding_model != ''
           ORDER BY created_at DESC, rowid DESC LIMIT 1''', (user_id,)).fetchone()
    conn.close()
    return (row['embedding_model'] if row else None) or DEFAULT_EMBEDDING_MODEL


def create_rag_tool(user_id, config):
    from services.llm_provider import get_user_api_key
    from rag_embeddings import embed_texts, EmbeddingError
    import rag_vector_store
    top_k = int(config.get('top_k') or 5)

    class RagQueryInput(BaseModel):
        query: str = Field(description='The question or search terms to look up in the uploaded documents')

    async def _run(query: str):
        api_key = get_user_api_key(user_id, 'google_genai')
        if not api_key:
            return ('No Google Gemini API key is saved, so uploaded documents cannot be searched. '
                    'Add one under Keys & Runs, or on an agent using the Google GenAI provider.')
        try:
            vector = embed_texts(api_key, _user_embedding_model(user_id), [query], 'RETRIEVAL_QUERY')[0]
            matches = rag_vector_store.query(user_id, vector, top_k)
        except (EmbeddingError, rag_vector_store.VectorStoreError) as error:
            return f'RAG search failed: {error}'
        if not matches:
            return 'No relevant chunks were found in the uploaded documents.'
        return '\n\n'.join(f"[{m.get('filename', 'document')} #{m.get('position', 0) + 1}] {m.get('text', '')}" for m in matches)

    return StructuredTool.from_function(coroutine=_run, name='rag_search', description='Search the current user\'s uploaded RAG documents for relevant context.', args_schema=RagQueryInput)

def create_builtin_tool(tool_type, config, user_id):
    if tool_type == 'duckduckgo':
        try:
            from langchain_community.tools.ddg_search.tool import DuckDuckGoSearchRun
            return DuckDuckGoSearchRun()
        except: return None
    if tool_type == 'rag':
        return create_rag_tool(user_id, config)
    from services.builtin_tools import create_builtin
    return create_builtin(tool_type, config)

def get_agent_tools(user_id, agent_id):
    from database import get_db
    conn = get_db()
    rows = conn.execute('''
        SELECT t.*, cts.api_url, cts.method, cts.headers, cts.request_body, cts.response_body,
               cts.path_params, cts.query_params, cts.auth_type, cts.auth_config
        FROM tools t JOIN tool_assignments ta ON t.id = ta.tool_id
        LEFT JOIN custom_tool_schemas cts ON t.id = cts.tool_id
        WHERE ta.agent_id = ? AND t.user_id = ?
    ''', (agent_id, user_id)).fetchall()
    conn.close()
    tools = []
    seen = set()
    for row in rows:
        tt = row['tool_type']
        # One malformed tool must not take the whole agent down with it; skip it
        # and let the agent run with the tools that do build.
        try:
            cfg = json.loads(row['config']) if isinstance(row['config'], str) else row['config']
            if tt == 'custom' and row['api_url']:
                schema = json.loads(row['request_body']) if isinstance(row['request_body'], str) else row['request_body']
                cts = {'api_url': row['api_url'], 'method': row['method'],
                       'headers': json.loads(row['headers']) if isinstance(row['headers'], str) else row['headers'],
                       'auth_type': row['auth_type'],
                       'auth_config': json.loads(row['auth_config']) if isinstance(row['auth_config'], str) else row['auth_config']}
                tool = create_custom_tool(row['name'], row['description'], schema, cts)
            else:
                tool = create_builtin_tool(tt, cfg, user_id)
        except Exception:
            logger.exception('Skipping tool %s (%s) that failed to build', row['name'], tt)
            continue
        # Two tools resolving to the same name would be ambiguous to the model.
        if tool and tool.name not in seen:
            seen.add(tool.name)
            tools.append(tool)
    return tools
