from fastapi import APIRouter, Depends, HTTPException, Query
from models import ToolCreate, ToolUpdate, CustomToolSchemaCreate, ToolAssignmentCreate
from database import get_db
from auth import current_user_id
import json, uuid
from datetime import datetime

router = APIRouter(prefix='/tools', tags=['tools'])

@router.get('')
async def list_tools(user_id: str = Depends(current_user_id), tool_type: str = None):
    conn = get_db()
    if tool_type:
        rows = conn.execute('SELECT * FROM tools WHERE user_id=? AND tool_type=? ORDER BY created_at DESC', (user_id, tool_type)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM tools WHERE user_id=? ORDER BY created_at DESC', (user_id,)).fetchall()
    tools = []
    for r in rows:
        t = dict(r)
        t['config'] = json.loads(t['config']) if isinstance(t['config'], str) else t['config']
        sr = conn.execute('SELECT * FROM custom_tool_schemas WHERE tool_id=?', (t['id'],)).fetchone()
        if sr:
            s = dict(sr)
            for k in ['headers','request_body','response_body','path_params','query_params','auth_config']:
                if isinstance(s.get(k), str): s[k] = json.loads(s[k])
            t['custom_schema'] = s
        else:
            t['custom_schema'] = None
        t['assigned_agents'] = [r2['agent_id'] for r2 in conn.execute('SELECT agent_id FROM tool_assignments WHERE tool_id=?', (t['id'],)).fetchall()]
        tools.append(t)
    conn.close()
    return tools

@router.get('/builtin')
async def list_builtin():
    return [
        {'type':'tavily','name':'Tavily Search','description':'AI-optimized search','config_fields':['api_key','max_results']},
        {'type':'google_search','name':'Google Search (Serper)','description':'Google via Serper API','config_fields':['api_key','max_results']},
        {'type':'duckduckgo','name':'DuckDuckGo Search','description':'Free web search','config_fields':[]},
        {'type':'github','name':'GitHub Actions','description':'GitHub repo interaction','config_fields':['api_key','repo','branch','action']},
        {'type':'rag','name':'RAG Document Search','description':'Search this user\'s uploaded documents (Hugging Face + Pinecone, managed automatically)','config_fields':['top_k']},
    ]

@router.post('')
async def create_tool(tool: ToolCreate, user_id: str = Depends(current_user_id)):
    tid = str(uuid.uuid4()); now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('INSERT INTO tools (id,user_id,name,description,tool_type,is_builtin,config,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                 (tid, user_id, tool.name, tool.description, tool.tool_type, int(tool.is_builtin), json.dumps(tool.config), now, now))
    conn.commit()
    r = dict(conn.execute('SELECT * FROM tools WHERE id=?', (tid,)).fetchone())
    r['config'] = json.loads(r['config']) if isinstance(r['config'], str) else r['config']
    r['custom_schema'] = None; r['assigned_agents'] = []
    conn.close()
    return r

@router.put('/{tool_id}')
async def update_tool(tool_id: str, tool: ToolUpdate, user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM tools WHERE id=? AND user_id=?', (tool_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Tool not found')
    updates = {k:v for k,v in tool.model_dump().items() if v is not None}
    if not updates: conn.close(); raise HTTPException(400, 'No fields')
    if 'config' in updates: updates['config'] = json.dumps(updates['config'])
    updates['updated_at'] = datetime.utcnow().isoformat()
    sc = ', '.join(f'{k}=?' for k in updates)
    conn.execute(f'UPDATE tools SET {sc} WHERE id=?', list(updates.values())+[tool_id])
    conn.commit()
    r = dict(conn.execute('SELECT * FROM tools WHERE id=?', (tool_id,)).fetchone())
    r['config'] = json.loads(r['config']) if isinstance(r['config'], str) else r['config']
    conn.close()
    return r

@router.delete('/{tool_id}')
async def delete_tool(tool_id: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM tools WHERE id=? AND user_id=?', (tool_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Tool not found')
    conn.execute('DELETE FROM tool_assignments WHERE tool_id=?', (tool_id,))
    conn.execute('DELETE FROM custom_tool_schemas WHERE tool_id=?', (tool_id,))
    conn.execute('DELETE FROM tools WHERE id=?', (tool_id,))
    conn.commit(); conn.close()
    return {'deleted': True}

@router.post('/{tool_id}/schema')
async def set_schema(tool_id: str, schema: CustomToolSchemaCreate, user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM tools WHERE id=? AND user_id=?', (tool_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Tool not found')
    now = datetime.utcnow().isoformat()
    conn.execute('''INSERT OR REPLACE INTO custom_tool_schemas (id,tool_id,api_url,method,headers,request_body,response_body,
        path_params,query_params,auth_type,auth_config,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (str(uuid.uuid4()), tool_id, schema.api_url, schema.method, json.dumps(schema.headers),
         json.dumps(schema.request_body), json.dumps(schema.response_body), json.dumps(schema.path_params),
         json.dumps(schema.query_params), schema.auth_type, json.dumps(schema.auth_config), now, now))
    conn.commit(); conn.close()
    return {'success': True}

@router.post('/assign')
async def assign(a: ToolAssignmentCreate, user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM tools WHERE id=? AND user_id=?', (a.tool_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Tool not found')
    if not conn.execute('SELECT id FROM agents WHERE id=? AND user_id=?', (a.agent_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Agent not found')
    try:
        conn.execute('INSERT INTO tool_assignments (id,agent_id,tool_id,created_at) VALUES (?,?,?,datetime(\'now\'))', (str(uuid.uuid4()), a.agent_id, a.tool_id))
        conn.commit()
    except: conn.close(); raise HTTPException(409, 'Already assigned')
    conn.close()
    return {'assigned': True}

@router.delete('/unassign')
async def unassign(agent_id: str = Query(...), tool_id: str = Query(...), user_id: str = Depends(current_user_id)):
    conn = get_db()
    if not conn.execute('SELECT id FROM tools WHERE id=? AND user_id=?', (tool_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Tool not found')
    if not conn.execute('SELECT id FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Agent not found')
    conn.execute('DELETE FROM tool_assignments WHERE agent_id=? AND tool_id=?', (agent_id, tool_id))
    conn.commit(); conn.close()
    return {'unassigned': True}
