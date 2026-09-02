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
    conns = []
    for r in conn.execute('SELECT * FROM agent_connections WHERE user_id=?', (user_id,)).fetchall():
        c = dict(r); c['condition'] = c.pop('condition_expr',''); conns.append(c)
    conn.close()
    return {'agents': agents, 'connections': conns}

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

@router.delete('/connections/{cid}')
async def delete_connection(cid: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    conn.execute('DELETE FROM agent_connections WHERE id=? AND user_id=?', (cid, user_id))
    conn.commit(); conn.close()
    return {'deleted': True}
