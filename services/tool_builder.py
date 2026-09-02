from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from typing import Any, Type
import json, aiohttp

def build_pydantic_schema(schema_dict, schema_name='ToolInput'):
    fields = {}
    for key, val in schema_dict.get('properties', {}).items():
        ft = str
        jt = val.get('type', 'string')
        if jt == 'number': ft = float
        elif jt == 'integer': ft = int
        elif jt == 'boolean': ft = bool
        elif jt == 'array': ft = list
        elif jt == 'object': ft = dict
        fd = val.get('default', ...)
        desc = val.get('description', '')
        if fd is not Ellipsis:
            fields[key] = (ft, Field(default=fd, description=desc))
        else:
            fields[key] = (ft, Field(description=desc))
    return type(schema_name, (BaseModel,), fields)

async def execute_custom_tool(api_url, method, headers, auth_type, auth_config, request_body):
    url = api_url
    req_headers = {**headers}
    if auth_type == 'bearer':
        req_headers['Authorization'] = f"Bearer {auth_config.get('token', '')}"
    elif auth_type == 'api_key':
        req_headers[auth_config.get('header_name', 'X-API-Key')] = auth_config.get('api_key', '')
    elif auth_type == 'basic':
        import base64
        cred = base64.b64encode(f"{auth_config.get('username','')}:{auth_config.get('password','')}".encode()).decode()
        req_headers['Authorization'] = f'Basic {cred}'
    try:
        async with aiohttp.ClientSession() as session:
            kw = {'headers': req_headers, 'timeout': aiohttp.ClientTimeout(total=30)}
            if method.upper() == 'GET':
                async with session.get(url, **kw) as resp: return json.dumps(await resp.json(), indent=2)
            elif method.upper() == 'POST':
                async with session.post(url, json=request_body, **{**kw, 'headers': {**req_headers, 'Content-Type': 'application/json'}}) as resp: return json.dumps(await resp.json(), indent=2)
            else:
                return json.dumps({'error': f'Unsupported method: {method}'})
    except Exception as e:
        return json.dumps({'error': str(e)})

def create_custom_tool(tool_name, tool_description, schema, cts):
    InputModel = build_pydantic_schema(schema, f'{tool_name}Input')
    async def _run(**kwargs):
        return await execute_custom_tool(cts['api_url'], cts['method'], cts.get('headers',{}), cts.get('auth_type','none'), cts.get('auth_config',{}), kwargs)
    return StructuredTool.from_function(coroutine=_run, name=tool_name, description=tool_description, args_schema=InputModel)

def create_rag_tool(user_id, config):
    from services.llm_provider import get_user_api_key
    from rag_embeddings import embed_texts, EmbeddingError, DEFAULT_EMBEDDING_MODEL
    import rag_vector_store
    top_k = int(config.get('top_k') or 5)

    class RagQueryInput(BaseModel):
        query: str = Field(description='The question or search terms to look up in the uploaded documents')

    async def _run(query: str):
        api_key = get_user_api_key(user_id, 'google_genai')
        if not api_key:
            return 'No Google Gemini API key is configured in Settings, so uploaded documents cannot be searched.'
        try:
            vector = embed_texts(api_key, DEFAULT_EMBEDDING_MODEL, [query], 'RETRIEVAL_QUERY')[0]
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
    for row in rows:
        tt = row['tool_type']
        cfg = json.loads(row['config']) if isinstance(row['config'], str) else row['config']
        if tt == 'custom' and row['api_url']:
            schema = json.loads(row['request_body']) if isinstance(row['request_body'], str) else row['request_body']
            cts = {'api_url': row['api_url'], 'method': row['method'],
                   'headers': json.loads(row['headers']) if isinstance(row['headers'], str) else row['headers'],
                   'auth_type': row['auth_type'],
                   'auth_config': json.loads(row['auth_config']) if isinstance(row['auth_config'], str) else row['auth_config']}
            tool = create_custom_tool(row['name'], row['description'], schema, cts)
            if tool: tools.append(tool)
        else:
            tool = create_builtin_tool(tt, cfg, user_id)
            if tool: tools.append(tool)
    return tools
