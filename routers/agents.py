from fastapi import APIRouter, Depends, HTTPException
from models import AgentCreate, AgentUpdate, ConnectionCreate
from database import get_db
from auth import current_user_id
from services.llm_provider import save_user_api_key
import json, uuid
from datetime import datetime

router = APIRouter(prefix='/agents', tags=['agents'])

def _row_to_agent(row, conn):
    a = dict(row)
    cur = conn.cursor()
    cur.execute('SELECT t.id,t.name,t.description,t.tool_type FROM tools t JOIN tool_assignments ta ON t.id=ta.tool_id WHERE ta.agent_id=?', (a['id'],))
    a['tools'] = [dict(r) for r in cur.fetchall()]
    cur.execute('SELECT id,source_agent_id,target_agent_id,label,condition_expr,created_at FROM agent_connections WHERE (source_agent_id=? OR target_agent_id=?) AND user_id=?', (a['id'],a['id'],a['user_id']))
    a['connections'] = [dict(r) for r in cur.fetchall()]
    # Lets the UI warn before run time that this agent's provider has no key.
    cur.execute('SELECT 1 FROM llm_configs WHERE user_id=? AND provider=? AND is_active=1 LIMIT 1', (a['user_id'], a['llm_provider']))
    a['has_api_key'] = cur.fetchone() is not None
    return a

@router.get('')
async def list_agents(user_id: str = Depends(current_user_id)):
    conn = get_db()
    rows = conn.execute('SELECT * FROM agents WHERE user_id=? ORDER BY created_at DESC', (user_id,)).fetchall()
    agents = [_row_to_agent(r, conn) for r in rows]
    conn.close()
    return agents

@router.get('/graph')
async def get_graph(user_id: str = Depends(current_user_id)):
    conn = get_db()
    agents = [dict(r) for r in conn.execute('SELECT * FROM agents WHERE user_id=?', (user_id,)).fetchall()]
    # Attach each agent's tools and key status in one pass - the graph nodes
    # show tool badges, and the UI warns when a provider has no key.
    tools_by_agent = {}
    for row in conn.execute(
        '''SELECT ta.agent_id, t.id, t.name, t.description, t.tool_type
           FROM tool_assignments ta JOIN tools t ON t.id = ta.tool_id WHERE t.user_id=?''', (user_id,)):
        entry = dict(row)
        tools_by_agent.setdefault(entry.pop('agent_id'), []).append(entry)
    keyed = {r['provider'] for r in conn.execute(
        'SELECT provider FROM llm_configs WHERE user_id=? AND is_active=1', (user_id,))}
    for agent in agents:
        agent['tools'] = tools_by_agent.get(agent['id'], [])
        agent['has_api_key'] = agent['llm_provider'] in keyed
    conns = []
    for r in conn.execute('SELECT * FROM agent_connections WHERE user_id=?', (user_id,)).fetchall():
        c = dict(r); c['condition'] = c.pop('condition_expr',''); conns.append(c)
    conn.close()
    return {'agents': agents, 'connections': conns}

# Anything that could be a credential is stripped on export: the file is
# downloaded and often shared, and everything here is re-enterable.
SECRET_KEYS = {'api_key', 'apikey', 'token', 'bot_token', 'webhook_url', 'password', 'secret', 'authorization'}
AGENT_FIELDS = ('name', 'description', 'system_prompt', 'llm_provider', 'llm_model',
                'temperature', 'max_tokens', 'is_sub_agent', 'position_x', 'position_y')


def _strip_secrets(value):
    if isinstance(value, dict):
        return {k: ('' if k.lower() in SECRET_KEYS else _strip_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def _loads(value, fallback):
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return fallback
    return value if value is not None else fallback


@router.get('/export')
async def export_graph(user_id: str = Depends(current_user_id)):
    """The whole workspace as portable JSON. Agents and tools are referenced by
    index rather than id so an import can create fresh rows."""
    conn = get_db()
    agent_rows = conn.execute('SELECT * FROM agents WHERE user_id=? ORDER BY created_at', (user_id,)).fetchall()
    agent_index = {row['id']: position for position, row in enumerate(agent_rows)}
    tool_rows = conn.execute('SELECT * FROM tools WHERE user_id=? ORDER BY created_at', (user_id,)).fetchall()
    tool_index = {row['id']: position for position, row in enumerate(tool_rows)}

    tools = []
    for row in tool_rows:
        entry = {'name': row['name'], 'description': row['description'], 'tool_type': row['tool_type'],
                 'is_builtin': bool(row['is_builtin']), 'config': _strip_secrets(_loads(row['config'], {}))}
        schema = conn.execute('SELECT * FROM custom_tool_schemas WHERE tool_id=?', (row['id'],)).fetchone()
        if schema:
            entry['custom_schema'] = {
                'api_url': schema['api_url'], 'method': schema['method'],
                'headers': _strip_secrets(_loads(schema['headers'], {})),
                'request_body': _loads(schema['request_body'], {}),
                'response_body': _loads(schema['response_body'], {}),
                'path_params': _loads(schema['path_params'], []),
                'query_params': _loads(schema['query_params'], []),
                'auth_type': schema['auth_type'],
                'auth_config': _strip_secrets(_loads(schema['auth_config'], {})),
            }
        tools.append(entry)

    connections = [{'source': agent_index[r['source_agent_id']], 'target': agent_index[r['target_agent_id']],
                    'label': r['label'], 'condition': r['condition_expr']}
                   for r in conn.execute('SELECT * FROM agent_connections WHERE user_id=?', (user_id,)).fetchall()
                   if r['source_agent_id'] in agent_index and r['target_agent_id'] in agent_index]

    assignments = [{'agent': agent_index[r['agent_id']], 'tool': tool_index[r['tool_id']]}
                   for r in conn.execute(
                       '''SELECT ta.agent_id, ta.tool_id FROM tool_assignments ta
                          JOIN agents a ON a.id = ta.agent_id WHERE a.user_id=?''', (user_id,)).fetchall()
                   if r['agent_id'] in agent_index and r['tool_id'] in tool_index]
    conn.close()

    return {
        'version': 1,
        'exported_at': datetime.utcnow().isoformat(),
        'note': 'API keys and other credentials are not included and must be re-entered after import.',
        'agents': [{field: dict(row)[field] for field in AGENT_FIELDS} for row in agent_rows],
        'connections': connections, 'tools': tools, 'assignments': assignments,
    }


@router.post('/import')
async def import_graph(payload: dict, user_id: str = Depends(current_user_id)):
    """Recreate an exported workspace alongside whatever is already there."""
    agents = payload.get('agents') or []
    if not isinstance(agents, list):
        raise HTTPException(400, 'Invalid file: "agents" must be a list')
    now = datetime.utcnow().isoformat()
    conn = get_db()
    agent_ids, tool_ids = [], []

    for entry in agents:
        if not isinstance(entry, dict) or not entry.get('name'):
            continue
        aid = str(uuid.uuid4())
        conn.execute('''INSERT INTO agents (id,user_id,name,description,system_prompt,llm_provider,llm_model,
            temperature,max_tokens,is_sub_agent,position_x,position_y,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (aid, user_id, str(entry.get('name'))[:200], entry.get('description') or '', entry.get('system_prompt') or '',
             entry.get('llm_provider') or 'openai', entry.get('llm_model') or 'gpt-4o',
             float(entry.get('temperature') or 0.7), int(entry.get('max_tokens') or 4096),
             int(bool(entry.get('is_sub_agent'))), float(entry.get('position_x') or 0), float(entry.get('position_y') or 0),
             now, now))
        agent_ids.append(aid)

    for entry in payload.get('tools') or []:
        if not isinstance(entry, dict) or not entry.get('name'):
            continue
        tid = str(uuid.uuid4())
        conn.execute('INSERT INTO tools (id,user_id,name,description,tool_type,is_builtin,config,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (tid, user_id, str(entry.get('name'))[:200], entry.get('description') or '',
                      entry.get('tool_type') or 'custom', int(bool(entry.get('is_builtin'))),
                      json.dumps(entry.get('config') or {}), now, now))
        tool_ids.append(tid)
        schema = entry.get('custom_schema')
        if isinstance(schema, dict) and schema.get('api_url'):
            conn.execute('''INSERT OR REPLACE INTO custom_tool_schemas (id,tool_id,api_url,method,headers,request_body,
                response_body,path_params,query_params,auth_type,auth_config,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (str(uuid.uuid4()), tid, schema['api_url'], schema.get('method') or 'POST',
                 json.dumps(schema.get('headers') or {}), json.dumps(schema.get('request_body') or {}),
                 json.dumps(schema.get('response_body') or {}), json.dumps(schema.get('path_params') or []),
                 json.dumps(schema.get('query_params') or []), schema.get('auth_type') or 'none',
                 json.dumps(schema.get('auth_config') or {}), now, now))

    def _ref(ids, index):
        return ids[index] if isinstance(index, int) and 0 <= index < len(ids) else None

    for entry in payload.get('connections') or []:
        source, target = _ref(agent_ids, (entry or {}).get('source')), _ref(agent_ids, (entry or {}).get('target'))
        if source and target and source != target:
            conn.execute('INSERT INTO agent_connections (id,user_id,source_agent_id,target_agent_id,label,condition_expr,created_at) VALUES (?,?,?,?,?,?,?)',
                         (str(uuid.uuid4()), user_id, source, target, entry.get('label') or '', entry.get('condition') or '', now))

    for entry in payload.get('assignments') or []:
        agent_ref, tool_ref = _ref(agent_ids, (entry or {}).get('agent')), _ref(tool_ids, (entry or {}).get('tool'))
        if agent_ref and tool_ref:
            conn.execute('INSERT OR IGNORE INTO tool_assignments (id,agent_id,tool_id,created_at) VALUES (?,?,?,?)',
                         (str(uuid.uuid4()), agent_ref, tool_ref, now))

    conn.commit(); conn.close()
    return {'imported': True, 'agents': len(agent_ids), 'tools': len(tool_ids)}


@router.post('')
async def create_agent(agent: AgentCreate, user_id: str = Depends(current_user_id)):
    aid = str(uuid.uuid4()); now = datetime.utcnow().isoformat()
    if agent.api_key:
        save_user_api_key(user_id, agent.llm_provider, agent.api_key, agent.base_url or '')
    conn = get_db()
    conn.execute('''INSERT INTO agents (id,user_id,name,description,system_prompt,llm_provider,llm_model,
        temperature,max_tokens,is_sub_agent,parent_id,position_x,position_y,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (aid, user_id, agent.name, agent.description, agent.system_prompt, agent.llm_provider, agent.llm_model,
         agent.temperature, agent.max_tokens, int(agent.is_sub_agent), agent.parent_id, agent.position_x, agent.position_y, now, now))
    conn.commit()
    result = _row_to_agent(conn.execute('SELECT * FROM agents WHERE id=?', (aid,)).fetchone(), conn)
    conn.close()
    return result

@router.get('/{agent_id}')
async def get_agent(agent_id: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    row = conn.execute('SELECT * FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone()
    if not row: conn.close(); raise HTTPException(404, 'Agent not found')
    result = _row_to_agent(row, conn)
    conn.close()
    return result

@router.put('/{agent_id}')
async def update_agent(agent_id: str, agent: AgentUpdate, user_id: str = Depends(current_user_id)):
    conn = get_db()
    existing = conn.execute('SELECT llm_provider FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone()
    if not existing:
        conn.close(); raise HTTPException(404, 'Agent not found')
    updates = {k:v for k,v in agent.model_dump().items() if v is not None}
    # These are provider credentials, not agent columns - route them to the
    # user's key store against whichever provider the agent ends up on.
    api_key = updates.pop('api_key', None)
    base_url = updates.pop('base_url', None)
    if api_key:
        save_user_api_key(user_id, updates.get('llm_provider') or existing['llm_provider'], api_key, base_url or '')
    if not updates: conn.close(); return await get_agent(agent_id, user_id)
    if 'is_sub_agent' in updates: updates['is_sub_agent'] = int(updates['is_sub_agent'])
    updates['updated_at'] = datetime.utcnow().isoformat()
    sc = ', '.join(f'{k}=?' for k in updates)
    conn.execute(f'UPDATE agents SET {sc} WHERE id=?', list(updates.values())+[agent_id])
    conn.commit()
    result = _row_to_agent(conn.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone(), conn)
    conn.close()
    return result

@router.delete('/{agent_id}')
async def delete_agent(agent_id: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Agent not found')
    conn.execute('DELETE FROM tool_assignments WHERE agent_id=?', (agent_id,))
    conn.execute('DELETE FROM agent_connections WHERE source_agent_id=? OR target_agent_id=?', (agent_id,agent_id))
    conn.execute('DELETE FROM agents WHERE id=?', (agent_id,))
    conn.commit(); conn.close()
    return {'deleted': True}

@router.post('/connections')
async def create_connection(c: ConnectionCreate, user_id: str = Depends(current_user_id)):
    cid = str(uuid.uuid4()); now = datetime.utcnow().isoformat()
    conn = get_db()
    owned = conn.execute('SELECT COUNT(*) AS n FROM agents WHERE id IN (?,?) AND user_id=?', (c.source_agent_id, c.target_agent_id, user_id)).fetchone()
    if owned['n'] != 2:
        conn.close(); raise HTTPException(404, 'Agent not found')
    conn.execute('INSERT INTO agent_connections (id,user_id,source_agent_id,target_agent_id,label,condition_expr,created_at) VALUES (?,?,?,?,?,?,?)',
                 (cid, user_id, c.source_agent_id, c.target_agent_id, c.label, c.condition, now))
    conn.commit()
    result = dict(conn.execute('SELECT * FROM agent_connections WHERE id=?', (cid,)).fetchone())
    result['condition'] = result.pop('condition_expr','')
    conn.close()
    return result

@router.put('/connections/{cid}')
async def update_connection(cid: str, body: dict, user_id: str = Depends(current_user_id)):
    """The label tells the source agent when to consult this target, so it ends
    up in the delegate tool's description."""
    conn = get_db()
    if not conn.execute('SELECT id FROM agent_connections WHERE id=? AND user_id=?', (cid, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Connection not found')
    conn.execute('UPDATE agent_connections SET label=?, condition_expr=? WHERE id=? AND user_id=?',
                 (str(body.get('label') or '')[:200], str(body.get('condition') or '')[:500], cid, user_id))
    conn.commit()
    result = dict(conn.execute('SELECT * FROM agent_connections WHERE id=?', (cid,)).fetchone())
    result['condition'] = result.pop('condition_expr', '')
    conn.close()
    return result


@router.delete('/connections/{cid}')
async def delete_connection(cid: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    conn.execute('DELETE FROM agent_connections WHERE id=? AND user_id=?', (cid, user_id))
    conn.commit(); conn.close()
    return {'deleted': True}
